"""Owner-scoped durable MCP 2.0 events with bounded, at-least-once delivery.

Adapted from wechat-dot-bridge events.py (MIT, copyright 2026 cd233).
See THIRD_PARTY_EVENTS_LICENSE.txt. Protocol: 2026-07-28.
https://developers.openai.com/plugins/build/mcp-events

Use one Events object on the same event loop as the owning SQLite Store.
Network I/O is async. SQLite transactions are short and never span await.
enqueue() MUST be called within the inbound message's existing transaction.
Only opaque message IDs enter event bodies; no QQ identifier or message text.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time

from .callback_transport import CallbackTransport
from .event_errors import EventError
from .webhook_signing import signed_headers, signing_key

EVENT_NAME = "qq.message"
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400
ROTATION_SECONDS = 300
VERIFICATION_SECONDS = 300
MAX_ATTEMPTS = 5
LEASE_SECONDS = 45


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class Events:
    def __init__(self, db: sqlite3.Connection, owner: str, allowed_hosts: tuple = (),
                 test_callback_origin: str | None = None, *, proxy=None):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise EventError("invalid_event_owner")
        self.db, self.owner = db, owner
        self.clock = time.time
        self.transport = CallbackTransport(allowed_hosts, test_callback_origin, proxy=proxy)
        self._network_lock = asyncio.Lock()
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
            db.execute("""CREATE INDEX IF NOT EXISTS event_delivery_due
                ON event_deliveries(status,next_attempt)""")

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
            "description": "A new accepted text message from the bound private owner or a sender allowed by the configured group trigger mode. Read the message ID to learn its conversation and privacy scope before replying.",
            "delivery": ["webhook"],
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "payloadSchema": {"type": "object", "properties": {"message_id": {"type": "string"}},
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

    async def subscribe(self, params):
        sid, url, secret = self._identity(params, True)
        requested = params.get("ttlMs", DEFAULT_TTL_SECONDS * 1000)
        if requested is not None and (type(requested) is not int or requested <= 0):
            raise EventError("invalid_params")
        # An explicit null grants no expiry. Otherwise honor the requested TTL
        # exactly up to the documented maximum; no hidden minimum lifetime.
        ttl = None if requested is None else min(requested / 1000, MAX_TTL_SECONDS)
        async with self._network_lock:
            now = self.clock()
            with self._transaction():
                self._expire(now)
                current = self.db.execute("""SELECT secret,old_secret,rotate_until,
                    verified_until,status FROM event_subscriptions WHERE id=? AND owner=?""",
                    (sid, self.owner)).fetchone()
            cached = bool(current and current[0] == secret and
                          current[3] > now and current[4] == "active")
            if not cached:
                challenge = secrets.token_urlsafe(32)
                body = _json_bytes({"type": "verification", "challenge": challenge})
                headers = signed_headers("verify_" + secrets.token_hex(16), body, secret, now, sid)
                status, response = await self.transport.post(url, body, headers)
                try:
                    value = json.loads(response)
                    echoed = value.get("challenge") if isinstance(value, dict) else None
                    valid = (isinstance(echoed, str) and hmac.compare_digest(
                        echoed.encode("utf-8"), challenge.encode("utf-8")))
                except (ValueError, UnicodeError, RecursionError):
                    valid = False
                if not (200 <= status < 300 and valid and self.clock() <= now + 30):
                    raise EventError("challenge_failed")
            now = self.clock()
            expires = None if ttl is None else now + ttl
            with self._transaction():
                self._expire(now)
                current = self.db.execute("""SELECT secret,old_secret,rotate_until,status
                    FROM event_subscriptions WHERE id=? AND owner=?""", (sid, self.owner)).fetchone()
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
        sid, _url, _secret = self._identity(params, False)
        async with self._network_lock:
            with self._transaction():
                self.db.execute("""UPDATE event_subscriptions SET status='cancelled',
                    secret='',old_secret=NULL,rotate_until=NULL WHERE id=? AND owner=?""", (sid, self.owner))
                self._expire(self.clock())
        return {}

    def enqueue(self, message_id: str, timestamp: int):
        if (not isinstance(message_id, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id) or
                type(timestamp) is not int or not 0 < timestamp < 253402300800):
            raise EventError("invalid_event_payload")
        if not self.db.in_transaction:
            raise EventError("event_transaction_required")
        now = self.clock()
        self._expire(now)
        rows = self.db.execute("SELECT id FROM event_subscriptions WHERE owner=? AND status='active'", (self.owner,)).fetchall()
        for (sid,) in rows:
            event_id = "evt_" + hashlib.sha256((sid + ":" + message_id).encode()).hexdigest()
            body = _json_bytes({"eventId": event_id, "name": EVENT_NAME, "timestamp": _iso(timestamp),
                                "data": {"message_id": message_id}, "cursor": None})
            self.db.execute("""INSERT OR IGNORE INTO event_deliveries
                (event_id,subscription_id,message_id,body,status,next_attempt)
                VALUES(?,?,?,?,'pending',?)""", (event_id, sid, message_id, body, now))
        return len(rows)

    async def deliver_once(self):
        async with self._network_lock:
            now = self.clock()
            with self._transaction():
                self._expire(now)
                # Unknown webhook receipt can safely retry the SAME event ID.
                # Lease recovery is also safe if a second process briefly exists.
                self.db.execute("""UPDATE event_deliveries SET
                    status=CASE WHEN attempts>=? THEN 'failed' ELSE 'retry' END,
                    next_attempt=?,lease_until=NULL,error='callback_interrupted'
                    WHERE status='sending' AND lease_until<=? AND subscription_id IN
                    (SELECT id FROM event_subscriptions WHERE owner=?)""",
                    (MAX_ATTEMPTS, now, now, self.owner))
                row = self.db.execute("""SELECT d.event_id,d.body,d.attempts,
                    s.id,s.url,s.secret,s.old_secret,s.rotate_until
                    FROM event_deliveries d JOIN event_subscriptions s ON s.id=d.subscription_id
                    WHERE s.owner=? AND s.status='active' AND d.status IN ('pending','retry')
                    AND d.next_attempt<=? AND d.attempts<?
                    ORDER BY d.next_attempt,d.rowid LIMIT 1""", (self.owner, now, MAX_ATTEMPTS)).fetchone()
                if not row:
                    return False
                event_id, body, attempts, sid, url, secret, old, rotate = row
                attempts += 1
                self.db.execute("""UPDATE event_deliveries SET status='sending',attempts=?,lease_until=?
                    WHERE event_id=?""", (attempts, now + LEASE_SECONDS, event_id))
            old = old if rotate and rotate > self.clock() else None
            headers = signed_headers(event_id, body, secret, self.clock(), sid, old)
            try:
                status, _response = await self.transport.post(url, body, headers)
                if 200 <= status < 300:
                    state, error = "acknowledged", None
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
                state = "retry" if error in {"callback_timeout", "callback_dns_failed",
                    "callback_network_failed"} else "failed"
            if state == "retry" and attempts >= MAX_ATTEMPTS:
                state, error = "failed", "retry_exhausted"
            with self._transaction():
                # Do not revive cancellation/expiry by an independent process.
                self.db.execute("""UPDATE event_deliveries SET status=?,error=?,
                    next_attempt=?,lease_until=NULL WHERE event_id=? AND status='sending'
                    AND attempts=?""", (state, error, self.clock() + min(300, 2 ** attempts), event_id, attempts))
                if error == "callback_gone":
                    self.db.execute("""UPDATE event_subscriptions SET status='cancelled',secret='',
                        old_secret=NULL,rotate_until=NULL WHERE id=? AND owner=?""", (sid, self.owner))
                self._expire(self.clock())
            return True

    def status(self):
        """Local safe diagnostics: no owner, callback URLs, secret, or message IDs."""
        with self._transaction():
            self._expire(self.clock())
            active = self.db.execute("SELECT COUNT(*) FROM event_subscriptions WHERE owner=? AND status='active'", (self.owner,)).fetchone()[0]
            counts = dict(self.db.execute("""SELECT d.status,COUNT(*) FROM event_deliveries d
                JOIN event_subscriptions s ON s.id=d.subscription_id WHERE s.owner=?
                GROUP BY d.status""", (self.owner,)).fetchall())
        return {"active_subscriptions": active, "deliveries": counts, "replay_supported": False}
