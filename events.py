"""Owner-scoped durable MCP 2.0 events with bounded, at-least-once delivery.

Adapted from wechat-dot-bridge events.py (MIT, copyright 2026 cd233).
See THIRD_PARTY_EVENTS_LICENSE.txt. Protocol: 2026-07-28.
https://developers.openai.com/plugins/build/mcp-events

Use one Events object on the same event loop as the owning SQLite Store.
Network I/O is async. SQLite transactions are short and never span await.
enqueue() MUST be called within the inbound message's existing transaction.
Only opaque message IDs and fixed plugin service notices enter event bodies;
no QQ identifier, filename, or user message text.
"""

import asyncio
from collections import OrderedDict, deque
from copy import deepcopy
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time

from .callback_transport import CallbackTransport
from .event_errors import EventError
from .service_notice import NOTICE_SCHEMA, file_service_notice
from .webhook_signing import signed_headers, signing_key

EVENT_NAME = "qq.message"
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400
MAX_ACTIVE_SUBSCRIPTIONS = 8
MAX_RETAINED_SUBSCRIPTIONS = 32
ROTATION_SECONDS = 300
VERIFICATION_SECONDS = 300
DIAGNOSTIC_ERRORS = frozenset({"invalid_params", "replay_not_supported", "invalid_signing_secret",
    "invalid_signing_payload", "callback_invalid", "callback_not_allowed", "callback_dns_blocked",
    "callback_dns_failed", "callback_encoding_unsupported", "callback_timeout", "callback_network_failed", "callback_tls_failed",
    "callback_redirect", "callback_response_too_large", "callback_proxy_failed", "callback_proxy_connect_failed", "callback_server_disconnected",
    "callback_connection_reset", "callback_response_incomplete", "callback_connector_failed", "challenge_failed",
    "event_too_large", "subscription_limit_reached", "callback_stopping", "subscription_cancelled", "event_operation_failed",
    "binding_inactive", "callback_preflight_rejected"})
MAX_ATTEMPTS = 5
LEASE_SECONDS = 45
SUBSCRIPTION_LOG_INTERVAL = 30
MAX_DELIVERY_LATENCY_SAMPLES = 32
DIAGNOSTIC_STAGES = frozenset({"validate_request", "waiting_for_callback_lock",
    "callback_verification", "persist_subscription", "active"})
DIAGNOSTIC_PHASES = frozenset({"starting", "proxy_tcp", "target_tcp", "connect_response",
    "tls", "request_write", "response_headers", "response_body", "connection_or_request"})
DIAGNOSTIC_DNS_PHASES = frozenset({"pending", "system", "queued", "connection_or_request",
    "response_body", "validation", "complete"})
DIAGNOSTIC_DNS_ERRORS = frozenset({"redirect", "http_status", "invalid_response", "response_too_large",
    "blocked", "timeout", "tls_failed", "proxy_failed", "response_incomplete", "network_failed",
    "stopping", "cancelled"})
DIAGNOSTIC_TLS_CATEGORIES = frozenset({"certificate_verification", "unexpected_eof",
    "zero_return", "ssl_error"})
DIAGNOSTIC_TLS_REASONS = frozenset({"certificate_verify_failed", "wrong_version",
    "protocol_version", "handshake_alert", "certificate_alert", "unexpected_eof", "other"})


def _log_enum(value, allowed, default="none"):
    return value if type(value) is str and value in allowed else default


def _log_number(value, minimum, maximum):
    return str(value) if type(value) is int and minimum <= value <= maximum else "none"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class Events:
    def __init__(self, db: sqlite3.Connection, owner: str, allowed_hosts: tuple = (),
                 test_callback_origin: str | None = None, *, proxy=None, callback_backend="aiohttp", callback_dns="system", logger=None, message_timing=None):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise EventError("invalid_event_owner")
        self.db, self.owner = db, owner
        self.clock = time.time
        self.logger = logger
        self.message_timing = message_timing
        # Injected by the endpoint. No watch or missing live opt-in means no reminders.
        self.processing_watch = None
        # Set only for sender-routed endpoints. An inactive parent can be a
        # startup barrier, so it pauses delivery without consuming queue retries.
        self.route_active = None
        self.delivery_authorize = None
        self._processing_observation_error = None
        self._reminder_lock = asyncio.Lock()
        self._last_subscription_log_at = None
        self.transport = CallbackTransport(allowed_hosts, test_callback_origin, proxy=proxy, backend=callback_backend, callback_dns=callback_dns)
        # Operations on one subscription serialize, without blocking callbacks
        # for other subscriptions. Entries live only while held or awaited.
        self._subscription_locks = {}
        self.delivery_wakeup = asyncio.Event()
        # Ephemeral measurements never change the durable queue schema or body.
        # Both pending origins and completed attempt samples have hard bounds.
        self._delivery_origins = OrderedDict()
        self._delivery_samples = deque(maxlen=MAX_DELIVERY_LATENCY_SAMPLES)
        # Deliberately not executescript(): it commits unrelated transactions.
        with self._transaction():
            db.execute("""CREATE TABLE IF NOT EXISTS event_subscriptions(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL,
                secret TEXT NOT NULL, old_secret TEXT, rotate_until REAL,
                expires_at REAL, verified_until REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','expired','cancelled')))
                """)
            db.execute("""CREATE TABLE IF NOT EXISTS event_deliveries(
                event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL
                  REFERENCES event_subscriptions(id), message_id TEXT NOT NULL,
                body BLOB NOT NULL, status TEXT NOT NULL CHECK(status IN
                  ('pending','sending','retry','acknowledged','failed','cancelled')),
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                lease_until REAL, error TEXT,
                UNIQUE(subscription_id,message_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS event_subscription_diagnostics(
                owner TEXT PRIMARY KEY, attempts INTEGER NOT NULL, attempted_at REAL NOT NULL,
                stage TEXT NOT NULL, error TEXT, http_status INTEGER)""")
            db.execute("""CREATE INDEX IF NOT EXISTS event_delivery_due
                ON event_deliveries(status,next_attempt)""")
            db.execute("""CREATE INDEX IF NOT EXISTS event_delivery_unresolved
                ON event_deliveries(subscription_id)
                WHERE status IN ('pending','retry','sending')""")

    def _diagnostic_start(self):
        with self._transaction():
            self.db.execute("""INSERT INTO event_subscription_diagnostics
                (owner,attempts,attempted_at,stage,error,http_status) VALUES(?,1,?,'validate_request',NULL,NULL)
                ON CONFLICT(owner) DO UPDATE SET attempts=MIN(attempts+1,2147483647),
                attempted_at=excluded.attempted_at,stage='validate_request',error=NULL,http_status=NULL""",
                (self.owner, self.clock()))
            return self.db.execute('SELECT attempts FROM event_subscription_diagnostics WHERE owner=?',
                                   (self.owner,)).fetchone()[0]

    def _diagnostic_update(self, attempt, stage, error=None, http_status=None):
        if error is not None and error not in DIAGNOSTIC_ERRORS:
            error = 'event_operation_failed'
        if type(http_status) is not int or not 100 <= http_status <= 599:
            http_status = None
        with self._transaction():
            self.db.execute("""UPDATE event_subscription_diagnostics SET stage=?,error=?,
                http_status=COALESCE(?,http_status) WHERE owner=? AND attempts=?""",
                (stage,error,http_status,self.owner,attempt))

    def _log_subscription(self, stage, error=None, transport=None):
        """One bounded, revalidated native log line; no callback/user identifiers."""
        try:
            if self.logger is None:
                return
            now = time.monotonic()
            if (self._last_subscription_log_at is not None and
                    now - self._last_subscription_log_at < SUBSCRIPTION_LOG_INTERVAL):
                return
            self._last_subscription_log_at = now
            attempts = transport.get('attempts') if type(transport) is dict else None
            detail = attempts[-1] if type(attempts) is list and attempts else None
            detail = detail if type(detail) is dict else {}
            fields = [
                "dot_private_subscription",
                "outcome=" + ("success" if error is None else "failure"),
                "stage=" + _log_enum(stage, DIAGNOSTIC_STAGES, "unknown"),
                "error=" + ("none" if error is None else
                            _log_enum(error, DIAGNOSTIC_ERRORS, "event_operation_failed")),
                "phase=" + _log_enum(detail.get('phase'), DIAGNOSTIC_PHASES),
                "elapsed_ms=" + _log_number(detail.get('elapsed_ms'), 0, 2147483647),
                "connect_status=" + _log_number(detail.get('connect_status'), 100, 599),
                "response_status=" + _log_number(detail.get('response_status'), 100, 599),
            ]
            if any(key in detail for key in ('tls_category', 'tls_verify_code', 'tls_reason')):
                fields.extend([
                    "tls_category=" + _log_enum(detail.get('tls_category'), DIAGNOSTIC_TLS_CATEGORIES),
                    "tls_verify_code=" + _log_number(detail.get('tls_verify_code'), 0, 65535),
                    "tls_reason=" + _log_enum(detail.get('tls_reason'), DIAGNOSTIC_TLS_REASONS),
                ])
            if 'response_encoding' in detail:
                fields.append('response_encoding=' + _log_enum(detail.get('response_encoding'),
                    {'identity','gzip','deflate','br','zstd','other'}))
            verification = transport.get('verification') if type(transport) is dict else None
            if type(verification) is dict:
                for key in ('json_valid','object_response','challenge_present','challenge_string',
                            'challenge_match','within_deadline','status_2xx'):
                    value = verification.get(key)
                    fields.append(key + '=' + ('true' if value is True else 'false' if value is False else 'unknown'))
            dns = transport.get('dns') if type(transport) is dict else None
            if type(dns) is dict:
                fields.extend([
                    "dns_mode=" + _log_enum(dns.get('mode'), {"system", "google"}),
                    "dns_phase=" + _log_enum(dns.get('phase'), DIAGNOSTIC_DNS_PHASES),
                    "dns_error=" + _log_enum(dns.get('error'), DIAGNOSTIC_DNS_ERRORS),
                    "dns_elapsed_ms=" + _log_number(dns.get('elapsed_ms'), 0, 2147483647),
                    "dns_http_status=" + _log_number(dns.get('http_status'), 100, 599),
                    "dns_connect_status=" + _log_number(dns.get('connect_status'), 100, 599),
                ])
            emit = self.logger.info if error is None else self.logger.warning
            emit(" ".join(fields))
        except Exception:
            # Diagnostics must never change the RPC result or cancellation.
            pass

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            raise EventError("event_transaction_busy")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    @asynccontextmanager
    async def _subscription_lock(self, sid):
        entry = self._subscription_locks.setdefault(sid, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._subscription_locks[sid]
            # A worker may have skipped this subscription while verification or
            # another delivery held its lock. Wake it after releasing the lock.
            self.delivery_wakeup.set()

    def _transport_details(self):
        # Real transports expose the current task's POST diagnostics. The
        # fallback keeps simple offline transport doubles compatible.
        return getattr(self.transport, 'request_transport',
                       getattr(self.transport, 'last_transport', None))

    def _subscription_capacity(self, current):
        """Check limits in the same short transaction that can activate a row."""
        if not current or current[-1] != 'active':
            active = self.db.execute("SELECT count(*) FROM event_subscriptions WHERE owner=? AND status='active'", (self.owner,)).fetchone()[0]
            if active >= MAX_ACTIVE_SUBSCRIPTIONS:
                raise EventError('subscription_limit_reached')
        if not current:
            stale = self.db.execute("SELECT id FROM event_subscriptions WHERE owner=? AND status!='active' ORDER BY rowid", (self.owner,)).fetchall()
            count = self.db.execute('SELECT count(*) FROM event_subscriptions WHERE owner=?', (self.owner,)).fetchone()[0]
            for (stale_id,) in stale:
                if count < MAX_RETAINED_SUBSCRIPTIONS:
                    break
                # A concurrent verification may still be renewing a stale row.
                if stale_id in self._subscription_locks:
                    continue
                self.db.execute('DELETE FROM event_deliveries WHERE subscription_id=?', (stale_id,))
                self.db.execute('DELETE FROM event_subscriptions WHERE id=?', (stale_id,))
                count -= 1
            if count >= MAX_RETAINED_SUBSCRIPTIONS:
                raise EventError('subscription_limit_reached')

    def _expire(self, now):
        self.db.execute("""UPDATE event_subscriptions SET status='expired',
            secret='',old_secret=NULL,rotate_until=NULL WHERE owner=? AND
            status='active' AND expires_at IS NOT NULL AND expires_at<=?""", (self.owner, now))
        self.db.execute("""UPDATE event_subscriptions SET old_secret=NULL,rotate_until=NULL
            WHERE owner=? AND rotate_until<=?""", (self.owner, now))
        self.db.execute("""UPDATE event_deliveries SET status='cancelled',
            error='subscription_inactive',lease_until=NULL WHERE status IN
            ('pending','retry','sending') AND subscription_id IN
            (SELECT id FROM event_subscriptions WHERE owner=? AND status!='active')""", (self.owner,))

    def list_events(self):
        return {"events": [{"name": EVENT_NAME,
            "description": "A new accepted message from an allowed private sender or a sender matching that group's configured rule. Read the message ID to learn its conversation and privacy scope before replying.",
            "delivery": ["webhook"],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "payloadSchema": {"type": "object", "properties": {"message_id": {"type": "string"},
                                  "service_notice": deepcopy(NOTICE_SCHEMA)},
                              "required": ["message_id"], "additionalProperties": False}}]}

    def _identity(self, params, with_secret):
        allowed = {"name", "arguments", "delivery", "_meta"}
        if with_secret:
            allowed |= {"cursor", "ttlMs"}
        if (not isinstance(params, dict) or set(params) - allowed or
                params.get("name") != EVENT_NAME or params.get("arguments", {}) != {}):
            raise EventError("invalid_params")
        if params.get("cursor") is not None:
            raise EventError("replay_not_supported")
        delivery = params.get("delivery")
        keys = {"mode", "url", "secret"} if with_secret else {"mode", "url"}
        if not isinstance(delivery, dict) or set(delivery) != keys or delivery.get("mode") != "webhook":
            raise EventError("invalid_params")
        url = delivery["url"]
        self.transport.validate(url)
        if with_secret:
            signing_key(delivery["secret"])
        identity = {"principal": self.owner, "url": url, "name": EVENT_NAME, "arguments": {}}
        sid = "sub_" + hashlib.sha256(_json_bytes(identity)).hexdigest()
        return sid, url, delivery.get("secret")

    def _route_permitted(self):
        if self.route_active is None:
            return True
        try:
            return callable(self.route_active) and self.route_active() is True
        except Exception:
            return False

    def _require_route(self):
        if not self._route_permitted():
            raise EventError('binding_inactive')

    def _delivery_permitted(self, message_id):
        if not self._route_permitted():
            return False
        if self.delivery_authorize is None:
            return True
        # Unexpected storage/validator failures are retryable, not evidence of
        # revoked permission. Let the owner pause rather than discard the row.
        return callable(self.delivery_authorize) and self.delivery_authorize(message_id) is True

    async def _post_callback(self, url, body, headers, *, preflight=None):
        if self.route_active is None:
            return await self.transport.post(url, body, headers)
        # The existing transport checks again after DNS and connection setup,
        # immediately before request bytes can leave the process.
        return await self.transport.post(url, body, headers,
                                         preflight=preflight or self._route_permitted)

    async def subscribe(self, params):
        attempt = self._diagnostic_start()
        state = {'stage': 'validate_request'}
        try:
            result = await self._subscribe(params, attempt, state)
            self._diagnostic_update(attempt, 'active')
            self._log_subscription('active', transport=state.get('transport'))
            return result
        except asyncio.CancelledError:
            self._diagnostic_update(attempt, state['stage'], 'subscription_cancelled')
            self._log_subscription(state['stage'], 'subscription_cancelled', state.get('transport'))
            raise
        except Exception as error:
            code = error.code if isinstance(error, EventError) else 'event_operation_failed'
            self._diagnostic_update(attempt, state['stage'], code, getattr(error, 'http_status', None))
            self._log_subscription(state['stage'], code, state.get('transport'))
            raise

    async def _subscribe(self, params, attempt, state):
        self._require_route()
        sid, url, secret = self._identity(params, True)
        requested = params.get("ttlMs", DEFAULT_TTL_SECONDS * 1000)
        if requested is not None and (type(requested) is not int or requested <= 0):
            raise EventError("invalid_params")
        # An explicit null grants no expiry. Otherwise honor the requested TTL
        # exactly up to the documented maximum; no hidden minimum lifetime.
        ttl = None if requested is None else min(requested / 1000, MAX_TTL_SECONDS)
        state['stage'] = 'waiting_for_callback_lock'
        self._diagnostic_update(attempt, state['stage'])
        async with self._subscription_lock(sid):
            self._require_route()
            now = self.clock()
            with self._transaction():
                self._expire(now)
                current = self.db.execute("""SELECT secret,old_secret,rotate_until,
                    verified_until,status FROM event_subscriptions WHERE id=? AND owner=?""",
                    (sid, self.owner)).fetchone()
                self._subscription_capacity(current)
            cached = bool(current and current[0] == secret and
                          current[3] > now and current[4] == "active")
            if not cached:
                state['stage'] = 'callback_verification'
                self._diagnostic_update(attempt, state['stage'])
                challenge = secrets.token_urlsafe(32)
                body = _json_bytes({"type": "verification", "challenge": challenge})
                headers = signed_headers("verify_" + secrets.token_hex(16), body, secret, now, sid)
                try:
                    status, response = await self._post_callback(url, body, headers)
                finally:
                    # Only this subscription's actual POST can supply transport
                    # details. Validation failures/cached renewals omit old state.
                    try:
                        state['transport'] = self._transport_details()
                    except Exception:
                        state['transport'] = None
                self._diagnostic_update(attempt, state['stage'], http_status=status)
                verification = dict(json_valid=False, object_response=False,
                    challenge_present=False, challenge_string=False, challenge_match=False,
                    within_deadline=False, status_2xx=200 <= status < 300)
                try:
                    value = json.loads(response)
                    verification['json_valid'] = True
                    verification['object_response'] = isinstance(value, dict)
                    verification['challenge_present'] = isinstance(value, dict) and 'challenge' in value
                    echoed = value.get('challenge') if isinstance(value, dict) else None
                    verification['challenge_string'] = isinstance(echoed, str)
                    verification['challenge_match'] = (isinstance(echoed, str) and hmac.compare_digest(
                        echoed.encode('utf-8'), challenge.encode('utf-8')))
                except (ValueError, UnicodeError, RecursionError):
                    pass
                verification['within_deadline'] = self.clock() <= now + 30
                transport_state = state.get('transport')
                if type(transport_state) is dict:
                    transport_state['verification'] = verification
                if not (verification['status_2xx'] and verification['challenge_match'] and verification['within_deadline']):
                    raise EventError('challenge_failed')
            self._require_route()
            now = self.clock()
            expires = None if ttl is None else now + ttl
            state['stage'] = 'persist_subscription'
            self._diagnostic_update(attempt, state['stage'])
            with self._transaction():
                self._expire(now)
                current = self.db.execute("""SELECT secret,old_secret,rotate_until,status
                    FROM event_subscriptions WHERE id=? AND owner=?""", (sid, self.owner)).fetchone()
                # Different subscriptions can finish verification concurrently.
                # Recheck limits here before activation, not only before I/O.
                self._subscription_capacity(current)
                continuous = bool(current and current[3] == "active")
                changed = continuous and current[0] != secret
                old = current[0] if changed else (current[1] if continuous else None)
                rotate = now + ROTATION_SECONDS if changed else (current[2] if continuous else None)
                verified = now + VERIFICATION_SECONDS if not cached else now
                # Cached refresh must NOT perpetually extend the verification cache.
                if cached:
                    verified = self.db.execute("SELECT verified_until FROM event_subscriptions WHERE id=?", (sid,)).fetchone()[0]
                self.db.execute("""INSERT INTO event_subscriptions
                    (id,owner,url,secret,old_secret,rotate_until,expires_at,verified_until,status)
                    VALUES(?,?,?,?,?,?,?,?,'active') ON CONFLICT(id) DO UPDATE SET
                    secret=excluded.secret,old_secret=excluded.old_secret,
                    rotate_until=excluded.rotate_until,expires_at=excluded.expires_at,
                    verified_until=excluded.verified_until,status='active'""",
                    (sid, self.owner, url, secret, old, rotate, expires, verified))
            return {"id": sid, "refreshBefore": None if expires is None else _iso(expires),
                    "cursor": None, "truncated": False}

    async def unsubscribe(self, params):
        self._require_route()
        sid, _url, _secret = self._identity(params, False)
        async with self._subscription_lock(sid):
            self._require_route()
            with self._transaction():
                self.db.execute("""UPDATE event_subscriptions SET status='cancelled',
                    secret='',old_secret=NULL,rotate_until=NULL WHERE id=? AND owner=?""", (sid, self.owner))
                self._expire(self.clock())
        return {}

    @staticmethod
    def _latency_stamp():
        return (time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000)

    @staticmethod
    def _latency_origin(value, latest):
        if (type(value) is tuple and len(value) == 2 and
                all(type(item) is int and 0 <= item <= ceiling
                    for item, ceiling in zip(value, latest))):
            return value
        return None

    @staticmethod
    def _latency_elapsed(start, end):
        # Never mix persisted wall clocks with monotonic process measurements.
        return end[1] - start[1] if start and end and start[1] <= end[1] else None

    @staticmethod
    def _latency_wall_age(now, due_at):
        if (type(now) not in (int, float) or type(due_at) not in (int, float) or
                not math.isfinite(now) or not math.isfinite(due_at)):
            return None
        seconds = now - due_at
        if seconds <= 0:
            return 0
        if seconds >= 2147483647 / 1000:
            return 2147483647
        return round(seconds * 1000)

    def notify_enqueued(self):
        # Callers using enqueue directly must commit before notifying.
        if not self.db.in_transaction:
            self.delivery_wakeup.set()

    def _record_delivery_latency(self, event_id, message_id, attempt, origin,
                                 stages, state, error, status, pending_wall_ms,
                                 transport):
        """Copy only bounded, explicitly safe telemetry from this POST."""
        try:
            timestamps = {**(origin or {'captured': None, 'ingest_started': None,
                                       'enqueued': None}), **stages}
            timestamps['callback_ack'] = stages['transport_finished'] if state == 'acknowledged' else None
            durations = {}
            for name, start, end in (
                    ('capture_to_enqueue', 'captured', 'enqueued'),
                    ('ingest_to_enqueue', 'ingest_started', 'enqueued'),
                    ('queue_to_claim', 'enqueued', 'claimed'),
                    ('callback_lock_wait', 'worker_started', 'lock_acquired'),
                    ('callback_total', 'transport_started', 'transport_finished'),
                    ('capture_to_callback_ack', 'captured', 'callback_ack'),
                    ('enqueue_to_callback_ack', 'enqueued', 'callback_ack')):
                durations[name] = self._latency_elapsed(timestamps.get(start), timestamps.get(end))
            transport = transport if type(transport) is dict else {}
            dns = transport.get('dns')
            dns = dns if type(dns) is dict else {}
            def bounded_number(value):
                return value if type(value) is int and 0 <= value <= 2147483647 else None
            durations['dns'] = bounded_number(dns.get('elapsed_ms'))
            attempts = transport.get('attempts')
            attempts = [item for item in attempts[:2] if type(item) is dict] if type(attempts) is list else []
            durations['callback_attempts'] = [bounded_number(item.get('elapsed_ms')) for item in attempts]
            def operation_stamp(detail, prefix):
                stamp = self._latency_origin((detail.get(prefix + '_utc_ms'),
                    detail.get(prefix + '_monotonic_ms')), stages['transport_finished'])
                return stamp if stamp and stamp[1] >= stages['transport_started'][1] else None
            timestamps['dns_started'] = operation_stamp(dns, 'started')
            timestamps['dns_completed'] = operation_stamp(dns, 'completed')
            callback_attempts = []
            for detail in attempts:
                callback_attempts.append({name: {'utc_ms': stamp[0], 'monotonic_ms': stamp[1]}
                    if (stamp := operation_stamp(detail, prefix)) else None
                    for name, prefix in (('started', 'started'), ('completed', 'completed'))})
            sample = {
                'event_id': event_id if re.fullmatch(r'evt_[a-f0-9]{64}', event_id) else None,
                'message_id': message_id if re.fullmatch(r'msg_[a-f0-9]{32}', message_id) else None,
                'attempt': attempt, 'outcome': state,
                'error': error if error in DIAGNOSTIC_ERRORS | {'callback_gone', 'callback_transient',
                    'callback_rejected', 'retry_exhausted', 'callback_interrupted'} else
                    ('event_operation_failed' if error else None),
                'http_status': status if type(status) is int and 100 <= status <= 599 else None,
                'timestamps': {name: {'utc_ms': stamp[0], 'monotonic_ms': stamp[1]} if stamp else None
                               for name, stamp in timestamps.items()},
                'durations_ms': durations,
                # Connection/request operation bounds, not request-wire timestamps.
                'callback_attempt_timestamps': callback_attempts,
                # Persisted due time supplies an approximate wall-clock queue age
                # only on the first attempt; it is never a measured duration.
                'pending_queue_wall_ms': pending_wall_ms,
                'dns_connection_reused': dns.get('connection_reused')
                    if type(dns.get('connection_reused')) is bool else None,
                'dns_connections_created': bounded_number(dns.get('connections_created')),
                'dns_connections_reused': bounded_number(dns.get('connections_reused')),
            }
            self._delivery_samples.append(sample)
            if self.message_timing is not None:
                self.message_timing.note_event(sample)
            if state in ('acknowledged', 'failed'):
                self._delivery_origins.pop(event_id, None)
        except Exception:
            # Diagnostics must not change delivery, retry, or cancellation.
            pass

    def _delivery_latency_status(self):
        samples = deepcopy(list(self._delivery_samples))
        summary = {}
        for key in ('queue_to_claim', 'callback_lock_wait', 'dns', 'callback_total', 'capture_to_callback_ack'):
            values = [sample['durations_ms'][key] for sample in samples
                      if sample['durations_ms'][key] is not None]
            summary[key] = {'count': len(values), 'mean': round(sum(values) / len(values), 1)
                            if values else None, 'max': max(values) if values else None}
        return {'limit': MAX_DELIVERY_LATENCY_SAMPLES, 'retained': len(samples),
                'summary_ms': summary, 'recent': samples}

    def enqueue(self, message_id: str, timestamp: int, *, captured_at=None, ingest_at=None, file_request=False):
        if (not isinstance(message_id, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id) or
                type(timestamp) is not int or not 0 < timestamp < 253402300800):
            raise EventError("invalid_event_payload")
        if not self.db.in_transaction:
            raise EventError("event_transaction_required")
        if type(file_request) is not bool:
            raise EventError("invalid_event_payload")
        data = {"message_id": message_id}
        if file_request:
            data["service_notice"] = file_service_notice(message_id)
        now = self.clock()
        self._expire(now)
        rows = self.db.execute("SELECT id FROM event_subscriptions WHERE owner=? AND status='active'", (self.owner,)).fetchall()
        enqueued_at = self._latency_stamp()
        for (sid,) in rows:
            event_id = "evt_" + hashlib.sha256((sid + ":" + message_id).encode()).hexdigest()
            body = _json_bytes({"eventId": event_id, "name": EVENT_NAME, "timestamp": _iso(timestamp),
                                "data": data, "cursor": None})
            inserted = self.db.execute("""INSERT OR IGNORE INTO event_deliveries
                (event_id,subscription_id,message_id,body,status,next_attempt)
                VALUES(?,?,?,?,'pending',?)""", (event_id, sid, message_id, body, now))
            if inserted.rowcount:
                self._delivery_origins[event_id] = {
                    'captured': self._latency_origin(captured_at, enqueued_at),
                    'ingest_started': self._latency_origin(ingest_at, enqueued_at),
                    'enqueued': enqueued_at}
                while len(self._delivery_origins) > MAX_DELIVERY_LATENCY_SAMPLES:
                    self._delivery_origins.popitem(last=False)
        return len(rows)

    def _delivery_subscription(self, now, *, include_locked=False):
        with self._transaction():
            self._expire(now)
            # Only a subscription's oldest unresolved event is eligible. A
            # retry backoff or unexpired lease blocks its own later events.
            rows = self.db.execute("""SELECT s.id FROM event_deliveries d
                JOIN event_subscriptions s ON s.id=d.subscription_id
                WHERE s.owner=? AND s.status='active'
                AND ((d.status IN ('pending','retry') AND d.next_attempt<=? AND d.attempts<?)
                     OR (d.status='sending' AND d.lease_until<=?))
                AND NOT EXISTS (SELECT 1 FROM event_deliveries prior
                    WHERE prior.subscription_id=d.subscription_id AND prior.rowid<d.rowid
                    AND prior.status IN ('pending','retry','sending'))
                ORDER BY d.next_attempt,d.rowid""", (self.owner, now, MAX_ATTEMPTS, now)).fetchall()
        return next((sid for (sid,) in rows
                     if include_locked or sid not in self._subscription_locks), None)

    async def deliver_once(self):
        if not self._route_permitted():
            return False
        worker_started = self._latency_stamp()
        sid = self._delivery_subscription(self.clock())
        if sid is None:
            return False
        # No await separates selecting an unlocked subscription and acquiring
        # its lock on the owning event loop. Other subscriptions stay available.
        async with self._subscription_lock(sid):
            if not self._route_permitted():
                return False
            lock_acquired = self._latency_stamp()
            now = self.clock()
            with self._transaction():
                self._expire(now)
                # Unknown webhook receipt can safely retry the SAME event ID.
                # Lease recovery is also safe if a second process briefly exists.
                self.db.execute("""UPDATE event_deliveries SET
                    status=CASE WHEN attempts>=? THEN 'failed' ELSE 'retry' END,
                    next_attempt=?,lease_until=NULL,error='callback_interrupted'
                    WHERE status='sending' AND lease_until<=? AND subscription_id=?""",
                    (MAX_ATTEMPTS, now, now, sid))
                row = self.db.execute("""SELECT d.event_id,d.body,d.attempts,
                    s.id,s.url,s.secret,s.old_secret,s.rotate_until,d.message_id,d.next_attempt
                    FROM event_deliveries d JOIN event_subscriptions s ON s.id=d.subscription_id
                    WHERE s.owner=? AND s.id=? AND s.status='active' AND d.status IN ('pending','retry')
                    AND d.next_attempt<=? AND d.attempts<?
                    AND NOT EXISTS (SELECT 1 FROM event_deliveries prior
                        WHERE prior.subscription_id=d.subscription_id AND prior.rowid<d.rowid
                        AND prior.status IN ('pending','retry','sending'))
                    ORDER BY d.rowid LIMIT 1""", (self.owner, sid, now, MAX_ATTEMPTS)).fetchone()
                if not row:
                    return False
                event_id, body, attempts, sid, url, secret, old, rotate, message_id, due_at = row
                if not self._route_permitted():
                    return False
                if not self._delivery_permitted(message_id):
                    # A live route with a revoked/deleted message is terminal.
                    # Retire this row so it cannot block later allowed messages.
                    self.db.execute("""UPDATE event_deliveries SET status='cancelled',
                        error='binding_inactive',lease_until=NULL WHERE event_id=?
                        AND status IN ('pending','retry')""", (event_id,))
                    return True
                pending_wall_ms = self._latency_wall_age(now, due_at) if attempts == 0 else None
                attempts += 1
                self.db.execute("""UPDATE event_deliveries SET status='sending',attempts=?,lease_until=?
                    WHERE event_id=?""", (attempts, now + LEASE_SECONDS, event_id))
            stages = {'worker_started': worker_started, 'lock_acquired': lock_acquired,
                      'claimed': self._latency_stamp()}
            origin = self._delivery_origins.get(event_id)
            old = old if rotate and rotate > self.clock() else None
            headers = signed_headers(event_id, body, secret, self.clock(), sid, old)
            previous_transport = self._transport_details()
            state, error, status = 'interrupted', 'callback_interrupted', None
            deferred = False
            acknowledged_at = None
            stages['transport_started'] = self._latency_stamp()
            if self.message_timing is not None:
                self.message_timing.note_event_started(message_id, event_id, attempts, stages)
            try:
                try:
                    status, _response = await self._post_callback(url, body, headers,
                        preflight=lambda: self._delivery_permitted(message_id))
                    if 200 <= status < 300:
                        state, error = "acknowledged", None
                        acknowledged_at = self.clock()
                    elif status == 410:
                        state, error = "failed", "callback_gone"
                    elif status == 413:
                        state, error = "failed", "event_too_large"
                    elif status in (408, 429) or 500 <= status < 600:
                        state, error = "retry", "callback_transient"
                    else:
                        state, error = "failed", "callback_rejected"
                except asyncio.CancelledError:
                    # Leave the immutable claimed event for bounded lease recovery.
                    raise
                except EventError as failure:
                    error = failure.code
                    if self.route_active is not None and error == 'callback_preflight_rejected':
                        # No request bytes were sent. Preserve the pending item
                        # across startup, apply and revocation barriers.
                        state, error, deferred = ('pending' if attempts == 1 else 'retry'), None, True
                        try:
                            revoked = self._route_permitted() and not self._delivery_permitted(message_id)
                        except Exception:
                            revoked = False
                        if revoked:
                            state, error = 'cancelled', 'binding_inactive'
                    else:
                        state = "retry" if error in {"callback_timeout", "callback_dns_failed",
                            "callback_network_failed", "callback_proxy_connect_failed",
                            "callback_server_disconnected", "callback_connection_reset",
                            "callback_response_incomplete", "callback_connector_failed"} else "failed"
                finally:
                    stages['transport_finished'] = self._latency_stamp()
                if not deferred and state == "retry" and attempts >= MAX_ATTEMPTS:
                    state, error = "failed", "retry_exhausted"
                with self._transaction():
                    # Do not revive cancellation/expiry by an independent process.
                    if deferred:
                        updated = self.db.execute("""UPDATE event_deliveries SET status=?,error=?,
                            attempts=attempts-1,next_attempt=?,lease_until=NULL
                            WHERE event_id=? AND status='sending' AND attempts=?""",
                            (state, error, due_at, event_id, attempts))
                    else:
                        updated = self.db.execute("""UPDATE event_deliveries SET status=?,error=?,
                            next_attempt=?,lease_until=NULL WHERE event_id=? AND status='sending'
                            AND attempts=?""", (state, error, self.clock() + min(300, 2 ** attempts), event_id, attempts))
                    if error == "callback_gone":
                        self.db.execute("""UPDATE event_subscriptions SET status='cancelled',secret='',
                            old_secret=NULL,rotate_until=NULL WHERE id=? AND owner=?""", (sid, self.owner))
                    self._expire(self.clock())
                if state == 'acknowledged' and updated.rowcount and self.processing_watch is not None:
                    # Commit the real ACK first: SQLITE_FULL during the optional
                    # observation can roll back an entire transaction, even when
                    # a nested savepoint was used. A gap means no reminder, never
                    # restoration of an acknowledged event to ordinary delivery.
                    try:
                        self.processing_watch.note_ack(message_id, event_id, at=acknowledged_at)
                        self._processing_observation_error = None
                    except Exception as failure:
                        self._processing_observation_error = ('storage_failed'
                            if isinstance(failure, sqlite3.Error) else 'observation_failed')
                return not deferred or state == 'cancelled'
            finally:
                current_transport = self._transport_details()
                # A failed POST must not inherit a previous verification's timing.
                self._record_delivery_latency(event_id, message_id, attempts, origin, stages,
                    state, error, status, pending_wall_ms,
                    current_transport if current_transport is not previous_transport else None)

    @staticmethod
    def _reminders_enabled(enabled):
        # Re-read the live flag; a truthy object or stale boolean is not opt-in.
        try:
            return callable(enabled) and enabled() is True
        except Exception:
            return False

    def _reminder_delivery(self, claim):
        """Read only the original acknowledged body and its still-active callback."""
        row = self.db.execute("""SELECT d.body,s.url,s.secret,s.old_secret,s.rotate_until
            FROM event_deliveries d JOIN event_subscriptions s ON s.id=d.subscription_id
            WHERE d.event_id=? AND d.message_id=? AND d.subscription_id=?
            AND d.status='acknowledged' AND s.owner=? AND s.status='active'
            AND (s.expires_at IS NULL OR s.expires_at>?)""",
            (claim['event_id'], claim['message_id'], claim['subscription_id'],
             self.owner, self.clock())).fetchone()
        if row is None:
            return None
        # Subscription identities bind the owner, event and exact callback URL.
        # Never follow a changed callback by accepting a mismatched stored ID.
        identity = {'principal': self.owner, 'url': row[1], 'name': EVENT_NAME, 'arguments': {}}
        expected = 'sub_' + hashlib.sha256(_json_bytes(identity)).hexdigest()
        return row if expected == claim['subscription_id'] else None

    async def remind_once(self, validator, *, enabled=None):
        """One bounded experimental re-wake, preserving the original delivery ACK.

        The watch owns persistent eligibility, claim limits and global spacing.
        A claimed attempt stays consumed after cancellation/failure. There is no
        reminder retry queue and no QQ-send path. Once POST starts, a later read
        or completion cannot retract the callback already in flight.
        """
        watch = self.processing_watch
        if (watch is None or not self._reminders_enabled(enabled) or
                not callable(validator) or self._reminder_lock.locked()):
            return False
        async with self._reminder_lock:
            # New/normal deliveries always get first choice of available locks.
            if self._delivery_subscription(self.clock(), include_locked=True) is not None:
                return False
            candidate = watch.candidate(enabled=self._reminders_enabled(enabled), owner=self.owner)
            if candidate is None:
                return False
            sid = candidate['subscription_id']
            if sid in self._subscription_locks:
                return False
            async with self._subscription_lock(sid):
                # Acquiring an unlocked subscription does not yield, but recheck
                # both priority and opt-in before consuming a durable attempt.
                if self._delivery_subscription(self.clock(), include_locked=True) is not None:
                    return False
                claim = watch.claim(candidate['event_id'],
                    enabled=self._reminders_enabled(enabled), owner=self.owner,
                    authorize=validator)
                if claim is None:
                    return False
                event_id, attempt = claim['event_id'], claim['attempt']
                outcome, error, status = 'skipped', None, None
                try:
                    # Revalidate after claim, immediately before the only await.
                    # All these checks are synchronous on the owning event loop.
                    if not watch.preflight(event_id, attempt,
                            enabled=self._reminders_enabled(enabled), owner=self.owner,
                            authorize=validator):
                        return True
                    row = self._reminder_delivery(claim)
                    if row is None or not self._reminders_enabled(enabled):
                        return True
                    body, url, secret, old, rotate = row
                    old = old if rotate and rotate > self.clock() else None
                    headers = signed_headers(event_id, body, secret, self.clock(), sid, old)
                    def preflight():
                        # DNS/connect can yield before any request bytes are sent.
                        # The transport invokes this again at its send boundary.
                        if self._delivery_subscription(self.clock(), include_locked=True) is not None:
                            return False
                        if not watch.preflight(event_id, attempt,
                                enabled=self._reminders_enabled(enabled), owner=self.owner,
                                authorize=validator):
                            return False
                        current = self._reminder_delivery(claim)
                        return (self._reminders_enabled(enabled) and current is not None
                                and current[0] == body and current[1] == url)
                    status, _response = await self.transport.post(url, body, headers,
                                                                  preflight=preflight)
                    if 200 <= status < 300:
                        outcome = 'acknowledged'
                    else:
                        outcome = 'failed'
                        error = ('callback_gone' if status == 410 else
                                 'event_too_large' if status == 413 else
                                 'callback_transient' if status in (408, 429) or 500 <= status < 600 else
                                 'callback_rejected')
                except asyncio.CancelledError:
                    outcome, error = 'interrupted', 'callback_interrupted'
                    raise
                except EventError as failure:
                    if failure.code == 'callback_preflight_rejected':
                        outcome, error = 'skipped', 'reminder_preflight_failed'
                    else:
                        outcome = 'failed'
                        error = failure.code if failure.code in DIAGNOSTIC_ERRORS else 'event_operation_failed'
                except Exception:
                    # Diagnostics contain a bounded category, never raw errors.
                    outcome, error = 'failed', 'event_operation_failed'
                finally:
                    try:
                        watch.finish(event_id, attempt, outcome=outcome,
                                     http_status=status, error=error)
                    except Exception as failure:
                        self._processing_observation_error = ('storage_failed'
                            if isinstance(failure, sqlite3.Error) else 'observation_failed')
                        # The committed claim still consumed its budget. Do not
                        # hide cancellation with an optional result-write error.
                        if outcome != 'interrupted':
                            raise
                return True

    def status(self):
        """Safe counts/timing; only opaque delivery IDs, never QQ IDs or contents."""
        with self._transaction():
            self._expire(self.clock())
            active = self.db.execute("SELECT COUNT(*) FROM event_subscriptions WHERE owner=? AND status='active'", (self.owner,)).fetchone()[0]
            counts = dict(self.db.execute("""SELECT d.status,COUNT(*) FROM event_deliveries d
                JOIN event_subscriptions s ON s.id=d.subscription_id WHERE s.owner=?
                GROUP BY d.status""", (self.owner,)).fetchall())
        row = self.db.execute("SELECT attempts,attempted_at,stage,error,http_status FROM event_subscription_diagnostics WHERE owner=?",
                              (self.owner,)).fetchone()
        diagnostic = None if row is None else {'attempts': row[0], 'attempted_at': _iso(row[1]),
            'stage': row[2], 'error': row[3], 'http_status': row[4]}
        callback_dns = getattr(self.transport, "callback_dns", "system")
        if callback_dns not in ("system", "google"):
            callback_dns = ""
        return {"active_subscriptions": active, "deliveries": counts, "replay_supported": False,
                "callback_transport": getattr(self.transport, "last_transport", None),
                "callback_backend": getattr(self.transport, "backend", "aiohttp"),
                "callback_dns": callback_dns,
                "delivery_latency": self._delivery_latency_status(),
                "processing_observation_error": self._processing_observation_error,
                "last_subscribe": diagnostic, "callback_proxy_enabled": bool(getattr(getattr(self.transport,'proxy',None),'enabled',False))}
