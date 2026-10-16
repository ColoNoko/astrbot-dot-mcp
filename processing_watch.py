"""Durable, bounded observations of ACK-to-read gaps in the existing inbox DB.

Only newly captured messages are enrolled. Unknown historical reads stay unknown.
A webhook ACK proves receipt, not a tool read or model processing. Experimental
reminders reuse one original event; a platform may deduplicate every reminder.
This module never sends QQ messages or performs network I/O. Its owner must use
one event-loop thread and must not span an await with a SQLite transaction.
"""
from contextlib import contextmanager
import inspect
import math
import re
import time

FIRST_REMINDER_SECONDS = 120
SECOND_REMINDER_SECONDS = 300
GLOBAL_REMINDER_INTERVAL_SECONDS = 30
MAX_REMINDERS = 2
MAX_RECENT = 32
_MESSAGE_ID = re.compile(r'msg_[a-f0-9]{32}\Z')
_EVENT_ID = re.compile(r'evt_[a-f0-9]{64}\Z')
_TOOLS = frozenset(('qq_read_message', 'qq_read_attachment', 'qq_reply',
                    'qq_reply_attachment', 'qq_complete_message'))
_OUTCOMES = frozenset(('acknowledged', 'failed', 'interrupted', 'skipped'))
_ERRORS = frozenset(('callback_timeout', 'callback_dns_failed', 'callback_network_failed',
    'callback_proxy_connect_failed', 'callback_server_disconnected', 'callback_connection_reset',
    'callback_response_incomplete', 'callback_connector_failed', 'callback_tls_failed',
    'callback_proxy_failed', 'callback_interrupted', 'callback_gone', 'event_too_large',
    'callback_transient', 'callback_rejected', 'callback_cancelled', 'callback_invalid',
    'callback_not_allowed', 'callback_dns_blocked', 'callback_redirect',
    'callback_response_too_large', 'callback_encoding_unsupported', 'event_operation_failed',
    'reminder_preflight_failed', 'reminder_disabled', 'callback_preflight_rejected'))


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _timestamp(value):
    return (float(value) if type(value) in (int, float) and
            0 <= value < 253402300800 and math.isfinite(value) else None)


def _dict(cursor):
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row is not None else None


class ProcessingWatch:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or time.time
        with self._write():
            db.execute('''CREATE TABLE IF NOT EXISTS message_processing_watch(
                message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                enrolled_at REAL NOT NULL, eligible_at_capture INTEGER NOT NULL CHECK(eligible_at_capture IN (0,1)),
                processing_started_at REAL, processing_tool TEXT,
                first_read_at REAL, attachment_started_at REAL, reply_claimed_at REAL,
                completed_at REAL, stopped_reason TEXT,
                first_ack_at REAL, event_id TEXT, subscription_id TEXT,
                reminder_attempts INTEGER NOT NULL DEFAULT 0 CHECK(reminder_attempts BETWEEN 0 AND 2),
                first_reminder_at REAL, last_reminder_at REAL,
                reminder_outcome TEXT, reminder_http_status INTEGER, reminder_error TEXT)''')
            db.execute('''CREATE INDEX IF NOT EXISTS message_processing_due
                ON message_processing_watch(first_ack_at,enrolled_at)
                WHERE eligible_at_capture=1 AND stopped_reason IS NULL AND reminder_attempts<2''')
            db.execute('''CREATE TABLE IF NOT EXISTS processing_watch_pacing(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_claim_at REAL)''')
            db.execute('INSERT OR IGNORE INTO processing_watch_pacing(singleton,last_claim_at) VALUES(1,NULL)')
            # These are safety backstops for any reply path, including failed or
            # uncertain sends. A later state change/deletion never re-enables it.
            db.execute('''CREATE TRIGGER IF NOT EXISTS processing_watch_reply_claim
                AFTER INSERT ON replies BEGIN
                UPDATE message_processing_watch SET
                    reply_claimed_at=COALESCE(reply_claimed_at,CAST(strftime('%s','now') AS REAL)),
                    stopped_reason=COALESCE(stopped_reason,'reply_claimed')
                WHERE message_id=NEW.message_id;
                END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS processing_watch_completion
                AFTER UPDATE OF completed_at ON messages WHEN NEW.completed_at>0 BEGIN
                UPDATE message_processing_watch SET
                    completed_at=COALESCE(completed_at,NEW.completed_at),
                    stopped_reason=COALESCE(stopped_reason,'completed')
                WHERE message_id=NEW.id;
                END''')

    def _ensure_subscription_triggers(self):
        # Subscription IDs are stable across cancellation/re-subscription at the
        # same callback URL. Inactivity must remain a terminal boundary for old
        # watches even when that same ID later becomes active again.
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS processing_watch_subscription_inactive
            AFTER UPDATE OF status ON event_subscriptions WHEN NEW.status!='active' BEGIN
            UPDATE message_processing_watch SET
                stopped_reason=COALESCE(stopped_reason,'subscription_inactive')
            WHERE subscription_id=NEW.id;
            END''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS processing_watch_subscription_deleted
            AFTER DELETE ON event_subscriptions BEGIN
            UPDATE message_processing_watch SET
                stopped_reason=COALESCE(stopped_reason,'subscription_inactive')
            WHERE subscription_id=OLD.id;
            END''')

    @contextmanager
    def _write(self):
        nested = self.db.in_transaction
        self.db.execute('SAVEPOINT processing_watch_write' if nested else 'BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                # SQLITE_FULL can roll back the whole transaction itself.
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK TO processing_watch_write')
                    self.db.execute('RELEASE processing_watch_write')
            else:
                self.db.rollback()
            raise
        else:
            if nested:
                self.db.execute('RELEASE processing_watch_write')
            else:
                self.db.commit()

    def _now(self):
        now = _timestamp(self.clock())
        if now is None:
            raise ValueError('invalid_processing_watch_clock')
        return now

    def enroll(self, message_id, *, eligible=False):
        """Call only for a newly INSERTed message, in its ingestion transaction."""
        if not _valid(message_id, _MESSAGE_ID):
            return False
        now = self._now()
        with self._write():
            inserted = self.db.execute('''INSERT OR IGNORE INTO message_processing_watch
                (message_id,enrolled_at,eligible_at_capture,reply_claimed_at,completed_at,stopped_reason)
                SELECT id,?,?,
                    CASE WHEN EXISTS(SELECT 1 FROM replies r WHERE r.message_id=m.id) THEN ? END,
                    CASE WHEN completed_at>0 THEN completed_at END,
                    CASE WHEN completed_at>0 THEN 'completed'
                         WHEN EXISTS(SELECT 1 FROM replies r WHERE r.message_id=m.id) THEN 'reply_claimed' END
                FROM messages m WHERE id=?''', (now, int(eligible is True), now, message_id))
        return bool(inserted.rowcount)

    def _observe(self, message_id, *, tool=None, column=None, reason=None):
        if not _valid(message_id, _MESSAGE_ID):
            return False
        now = self._now()
        assignments, args = [], []
        if type(tool) is str and tool in _TOOLS:
            assignments.extend(('processing_started_at=COALESCE(processing_started_at,?)',
                                'processing_tool=COALESCE(processing_tool,?)'))
            args.extend((now, tool))
        if column is not None:
            # Only internal fixed column names are passed to this helper.
            assignments.append(f'{column}=COALESCE({column},?)')
            args.append(now)
        if reason is not None:
            assignments.append('stopped_reason=COALESCE(stopped_reason,?)')
            args.append(reason)
        if not assignments:
            return False
        with self._write():
            changed = self.db.execute('UPDATE message_processing_watch SET ' + ','.join(assignments) +
                                     ' WHERE message_id=?', (*args, message_id))
        return bool(changed.rowcount)

    def note_started(self, message_id, tool):
        if tool == 'qq_read_attachment':
            return self.note_attachment(message_id)
        return self._observe(message_id, tool=tool)

    def note_read(self, message_id):
        return self._observe(message_id, tool='qq_read_message', column='first_read_at', reason='read')

    def note_attachment(self, message_id):
        return self._observe(message_id, tool='qq_read_attachment',
                             column='attachment_started_at', reason='attachment_processing')

    def note_reply_claim(self, message_id):
        return self._observe(message_id, column='reply_claimed_at', reason='reply_claimed')

    def note_complete(self, message_id):
        return self._observe(message_id, column='completed_at', reason='completed')

    def note_ack(self, message_id, event_id, *, at=None):
        """Preserve the first actual ACK and original subscription, never infer it."""
        if not _valid(message_id, _MESSAGE_ID) or not _valid(event_id, _EVENT_ID):
            return False
        now = self._now() if at is None else _timestamp(at)
        if now is None:
            return False
        with self._write():
            self._ensure_subscription_triggers()
            changed = self.db.execute('''UPDATE message_processing_watch SET
                first_ack_at=?,event_id=?,subscription_id=(SELECT subscription_id
                    FROM event_deliveries WHERE event_id=?)
                WHERE message_id=? AND first_ack_at IS NULL AND EXISTS(
                    SELECT 1 FROM event_deliveries d WHERE d.event_id=?
                    AND d.message_id=message_processing_watch.message_id AND d.status='acknowledged')''',
                (now, event_id, event_id, message_id, event_id))
        return bool(changed.rowcount)

    def _eligible_sql(self, *, owner=None):
        sql = '''SELECT w.event_id,w.message_id,w.subscription_id,w.reminder_attempts,
                w.first_ack_at,w.first_reminder_at,w.last_reminder_at,w.reminder_outcome
            FROM message_processing_watch w
            JOIN messages m ON m.id=w.message_id
            JOIN event_deliveries d ON d.event_id=w.event_id AND d.message_id=w.message_id
                AND d.subscription_id=w.subscription_id
            JOIN event_subscriptions s ON s.id=w.subscription_id
            WHERE w.eligible_at_capture=1 AND w.stopped_reason IS NULL
                AND w.first_read_at IS NULL AND w.attachment_started_at IS NULL
                AND w.reply_claimed_at IS NULL AND w.completed_at IS NULL
                AND w.first_ack_at IS NOT NULL AND m.completed_at=0
                AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.message_id=w.message_id)
                AND d.status='acknowledged' AND s.status='active'
                AND (s.expires_at IS NULL OR s.expires_at>?)'''
        args = [self._now()]
        if owner is not None:
            sql += ' AND s.owner=?'
            args.append(owner)
        return sql, args

    def _due_sql(self, *, owner=None):
        sql, args = self._eligible_sql(owner=owner)
        now = self._now()
        sql += ''' AND w.reminder_attempts<2
            AND ((w.reminder_attempts=0 AND w.first_ack_at+?<=?)
                 OR (w.reminder_attempts=1 AND w.first_reminder_at+?<=?))
            AND EXISTS(SELECT 1 FROM processing_watch_pacing p WHERE p.singleton=1
                AND (p.last_claim_at IS NULL OR p.last_claim_at+?<=?))'''
        args.extend((FIRST_REMINDER_SECONDS, now, SECOND_REMINDER_SECONDS, now,
                     GLOBAL_REMINDER_INTERVAL_SECONDS, now))
        return sql, args

    @staticmethod
    def _candidate(row):
        return {key: row[key] for key in ('event_id', 'message_id', 'subscription_id')}

    def candidate(self, *, enabled=False, owner=None):
        if enabled is not True:
            return None
        sql, args = self._due_sql(owner=owner)
        row = _dict(self.db.execute(sql + ' ORDER BY w.first_ack_at,w.enrolled_at,w.message_id LIMIT 1', args))
        return self._candidate(row) if row is not None else None

    @staticmethod
    def _authorized(authorize, message_id):
        # A missing validator must fail closed; enrollment is not live permission.
        if not callable(authorize):
            return False
        try:
            result = authorize(message_id)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                return False
            return result is True
        except Exception:
            return False

    def claim(self, event_id, *, enabled=False, owner=None, authorize=None):
        """Commit budget before POST. Failed/interrupted/skipped calls consume it."""
        if enabled is not True or not _valid(event_id, _EVENT_ID):
            return None
        if self.db.in_transaction:
            raise ValueError('processing_watch_transaction_busy')
        with self._write():
            sql, args = self._due_sql(owner=owner)
            row = _dict(self.db.execute(sql + ' ORDER BY w.first_ack_at,w.enrolled_at,w.message_id LIMIT 1', args))
            if row is None or row['event_id'] != event_id:
                return None
            if not self._authorized(authorize, row['message_id']):
                self._observe(row['message_id'], reason='authorization_revoked')
                return None
            # Authorization hooks are synchronous, but may inspect/change local
            # state. Recheck after them without yielding to another coroutine.
            sql, args = self._due_sql(owner=owner)
            row = _dict(self.db.execute(sql + ' ORDER BY w.first_ack_at,w.enrolled_at,w.message_id LIMIT 1', args))
            if row is None or row['event_id'] != event_id:
                return None
            now = self._now()
            attempt = row['reminder_attempts'] + 1
            self.db.execute('''UPDATE message_processing_watch SET reminder_attempts=?,
                first_reminder_at=COALESCE(first_reminder_at,?),last_reminder_at=?,
                reminder_outcome='claimed',reminder_http_status=NULL,reminder_error=NULL
                WHERE event_id=? AND message_id=?''',
                (attempt, now, now, event_id, row['message_id']))
            self.db.execute('UPDATE processing_watch_pacing SET last_claim_at=? WHERE singleton=1', (now,))
        return {**self._candidate(row), 'attempt': attempt}

    def preflight(self, event_id, attempt, *, enabled=False, owner=None, authorize=None):
        if (enabled is not True or not _valid(event_id, _EVENT_ID) or
                type(attempt) is not int or not 1 <= attempt <= MAX_REMINDERS):
            return False
        sql, args = self._eligible_sql(owner=owner)
        sql += " AND w.event_id=? AND w.reminder_attempts=? AND w.reminder_outcome='claimed'"
        args.extend((event_id, attempt))
        row = _dict(self.db.execute(sql, args))
        if row is None:
            return False
        if not self._authorized(authorize, row['message_id']):
            self._observe(row['message_id'], reason='authorization_revoked')
            return False
        sql, args = self._eligible_sql(owner=owner)
        sql += " AND w.event_id=? AND w.reminder_attempts=? AND w.reminder_outcome='claimed'"
        return self.db.execute(sql, (*args, event_id, attempt)).fetchone() is not None

    def finish(self, event_id, attempt, *, outcome, http_status=None, error=None):
        if (not _valid(event_id, _EVENT_ID) or type(attempt) is not int or
                not 1 <= attempt <= MAX_REMINDERS):
            return False
        outcome = outcome if type(outcome) is str and outcome in _OUTCOMES else 'failed'
        if type(http_status) is not int or not 100 <= http_status <= 599:
            http_status = None
        if error is not None and (type(error) is not str or error not in _ERRORS):
            error = 'event_operation_failed'
        with self._write():
            changed = self.db.execute('''UPDATE message_processing_watch SET
                reminder_outcome=?,reminder_http_status=?,reminder_error=?
                WHERE event_id=? AND reminder_attempts=? AND reminder_outcome='claimed' ''',
                (outcome, http_status, error, event_id, attempt))
        return bool(changed.rowcount)

    @staticmethod
    def _snapshot(row):
        read, ack = row['first_read_at'], row['first_ack_at']
        if row['completed_at'] is not None:
            observed = 'completed'
        elif row['reply_claimed_at'] is not None:
            observed = 'reply_claimed'
        elif read is not None:
            observed = 'read'
        elif row['attachment_started_at'] is not None:
            observed = 'attachment_processing'
        elif row['processing_started_at'] is not None:
            observed = 'processing_started'
        else:
            observed = 'no_processing_observed'
        due = None
        if row['eligible_at_capture'] and row['stopped_reason'] is None and row['reminder_attempts'] < 2:
            due = (ack + FIRST_REMINDER_SECONDS if row['reminder_attempts'] == 0 and ack is not None
                   else row['first_reminder_at'] + SECOND_REMINDER_SECONDS
                   if row['first_reminder_at'] is not None else None)
        # IDs are opaque generated identifiers only. No body, callback, secret,
        # QQ identifiers, reply keys, or arbitrary diagnostic text is returned.
        return {'message_id': row['message_id'], 'observation': 'tracked',
            'processing_state': observed, 'enrolled_at': row['enrolled_at'],
            'processing_started_at': row['processing_started_at'], 'processing_tool': row['processing_tool'],
            'first_read_at': read, 'attachment_started_at': row['attachment_started_at'],
            'reply_claimed_at': row['reply_claimed_at'], 'completed_at': row['completed_at'],
            'first_ack_at': ack, 'event_id': row['event_id'],
            'unobserved_ack_to_first_read_wall_ms': round((read-ack)*1000)
                if ack is not None and read is not None and read >= ack else None,
            'eligible_at_capture': bool(row['eligible_at_capture']), 'stopped_reason': row['stopped_reason'],
            'reminder_attempts': row['reminder_attempts'], 'max_reminders': MAX_REMINDERS,
            'first_reminder_at': row['first_reminder_at'], 'last_reminder_at': row['last_reminder_at'],
            'next_reminder_due_at': due, 'reminder_outcome': row['reminder_outcome'],
            'reminder_http_status': row['reminder_http_status'], 'reminder_error': row['reminder_error']}

    def get(self, message_id):
        if not _valid(message_id, _MESSAGE_ID):
            return None
        row = _dict(self.db.execute('SELECT * FROM message_processing_watch WHERE message_id=?', (message_id,)))
        if row is None:
            return {'message_id': message_id, 'observation': 'unknown', 'processing_state': 'unknown',
                    'first_ack_at': None, 'first_read_at': None, 'eligible_at_capture': False,
                    'reminder_attempts': 0, 'max_reminders': MAX_REMINDERS}
        return self._snapshot(row)

    def status(self, *, enabled=False, limit=MAX_RECENT):
        limit = min(limit, MAX_RECENT) if type(limit) is int and limit > 0 else MAX_RECENT
        cursor = self.db.execute('SELECT * FROM message_processing_watch ORDER BY enrolled_at DESC,message_id DESC LIMIT ?', (limit,))
        columns = [item[0] for item in cursor.description]
        recent = [self._snapshot(dict(zip(columns, row))) for row in cursor.fetchall()]
        tracked = self.db.execute('SELECT COUNT(*) FROM message_processing_watch').fetchone()[0]
        unknown = self.db.execute('''SELECT COUNT(*) FROM messages m WHERE NOT EXISTS(
            SELECT 1 FROM message_processing_watch w WHERE w.message_id=m.id)''').fetchone()[0]
        return {'enabled': enabled is True, 'observations_enabled': True, 'tracked_messages': tracked,
                'unknown_messages': unknown, 'max_reminders': MAX_REMINDERS,
                'first_reminder_seconds': FIRST_REMINDER_SECONDS,
                'second_reminder_seconds': SECOND_REMINDER_SECONDS,
                'global_min_interval_seconds': GLOBAL_REMINDER_INTERVAL_SECONDS,
                'same_event_only': True, 'redelivery_guaranteed': False,
                'limit': MAX_RECENT, 'recent': recent}
