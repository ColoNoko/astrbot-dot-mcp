"""Bounded, conversation-scoped SQLite history for the host-owned inbox.

The durable database plus any WAL is limited to exactly 10 MiB (10,485,760
bytes).  Rollback journaling is deliberately used instead of WAL: SQLite's
journal_size_limit is not a hard WAL limit.  Rollback journals and operator-
created backups can require additional temporary disk space.  This is not a
whole-directory quota.  All calls run synchronously on the Store's one SQLite
connection/event-loop thread; no transaction may span an await.

Only explicitly completed work, or a successful adapter reply, can be pruned.
Reads and event webhook acknowledgments are never completion.  The finite
source/reply tombstones are best-effort replay protection for the documented
retention window, not an everlasting idempotency guarantee.
"""

if __package__:
    from .reply_metadata import display_reply
else:
    from reply_metadata import display_reply

import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

HISTORY_LIMIT_BYTES = 10 * 1024 * 1024
CONTROL_RESERVE_BYTES = 64 * 1024
PENDING_REPLY_RESERVE_BYTES = 64 * 1024
MAX_TOMBSTONES = 2048
TOMBSTONE_TTL_SECONDS = 7 * 24 * 3600
MAX_HISTORY_PAGE = 100
MAX_CONVERSATION_PAGE = 50


def _raise(error_cls, code):
    raise error_cls(code)


def storage_error(error):
    """Return a safe storage error code, without SQLite paths or payloads."""
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None and (code & 255) == sqlite3.SQLITE_FULL:
        return 'storage_full'
    if code is not None and (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return 'storage_busy'
    return 'storage_failed'


def _file_bytes(path):
    try:
        return Path(path).stat().st_size
    except FileNotFoundError:
        return 0


def configure_storage(db, path, max_bytes=HISTORY_LIMIT_BYTES, error_cls=ValueError):
    """Configure the hard database limit without deleting or replacing data.

    Run after validating the database binding, before schema migrations/writes.
    An already oversized legacy database is refused intact.  SQLite itself
    checkpoints/removes WAL when changing journal mode; never unlink sidecars.
    An exclusive startup transaction detects active readers/writers.  As with
    Store itself, the database must have one owning plugin process.
    """
    if type(max_bytes) is not int or max_bytes < 128 * 1024:
        _raise(error_cls, 'invalid_history_limit')
    if db.in_transaction:
        _raise(error_cls, 'storage_transaction_busy')
    path = Path(path)
    page_size = db.execute('PRAGMA page_size').fetchone()[0]
    pages = db.execute('PRAGMA page_count').fetchone()[0]
    limit_pages = max_bytes // page_size
    # Check before checkpoint, mode conversion, schema changes, or deletion.
    if (pages > limit_pages or
            _file_bytes(path) + _file_bytes(str(path) + '-wal') > max_bytes):
        _raise(error_cls, 'history_migration_requires_space')
    try:
        mode = db.execute('PRAGMA journal_mode').fetchone()[0].lower()
        if mode == 'wal':
            busy, _log, _done = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy:
                _raise(error_cls, 'history_migration_busy')
        mode = db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()
        if mode != 'delete':
            _raise(error_cls, 'history_migration_busy')
        # journal_mode=DELETE takes SQLite's own exclusive conversion lock.
        # The additional transaction also checks an already-DELETE database.
        db.execute('BEGIN EXCLUSIVE')
        actual_limit = db.execute(f'PRAGMA max_page_count={limit_pages}').fetchone()[0]
        if actual_limit != limit_pages:
            _raise(error_cls, 'history_migration_requires_space')
        db.commit()
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
    except sqlite3.Error as error:
        db.rollback()
        code = storage_error(error)
        _raise(error_cls, 'history_migration_busy' if code == 'storage_busy' else code)
    except BaseException:
        db.rollback()
        raise
    if _file_bytes(path) + _file_bytes(str(path) + '-wal') > max_bytes:
        _raise(error_cls, 'history_migration_requires_space')
    return limit_pages * page_size


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).digest()


def _identity(value):
    return [value['conversation_type'], value['group_id'], value['sender_id'], value['source_id']]


def _content(value):
    # The original canonical media descriptor JSON belongs in the dedupe
    # fingerprint too, when the media extension adds it to message snapshots.
    media = value.get('media_json', value.get('attachments', []))
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except (ValueError, TypeError):
            pass
    return [value['umo'], value['text'], media]


class BoundedHistory:
    def __init__(self, db, path, *, error_cls=ValueError,
                 max_bytes=HISTORY_LIMIT_BYTES, control_reserve=CONTROL_RESERVE_BYTES,
                 pending_reply_reserve=PENDING_REPLY_RESERVE_BYTES,
                 max_tombstones=MAX_TOMBSTONES, tombstone_ttl=TOMBSTONE_TTL_SECONDS,
                 policy_provider=None, privacy_scope=None, private_sender_id=None):
        self.db, self.path, self.error_cls = db, Path(path), error_cls
        self.policy_provider = policy_provider
        self.privacy_scope = privacy_scope
        self.private_sender_id = private_sender_id
        self.max_bytes = max_bytes
        self.control_reserve, self.pending_reply_reserve = control_reserve, pending_reply_reserve
        self.max_tombstones, self.tombstone_ttl = max_tombstones, tombstone_ttl
        self.clock = time.time
        self.last_error = None
        self.rejected_count = 0
        self.pruned_count = 0
        if (any(type(v) is not int or v < 0 for v in
                (control_reserve, pending_reply_reserve, max_tombstones, tombstone_ttl)) or
                max_tombstones > MAX_TOMBSTONES or tombstone_ttl > TOMBSTONE_TTL_SECONDS):
            self._fail('invalid_history_configuration')
        self._ensure_schema()

    def note_storage_error(self, error):
        """Record a fixed safe code when a shared-table write fails."""
        code = storage_error(error) if isinstance(error, sqlite3.Error) else str(error)
        if code not in ('storage_full', 'storage_busy', 'storage_failed'):
            code = 'storage_failed'
        self.last_error = code
        if code == 'storage_full':
            self.rejected_count += 1
        return code

    def _fail(self, code):
        if code.startswith(('storage_', 'history_migration_')):
            self.last_error = code
        if code == 'storage_full':
            self.rejected_count += 1
        _raise(self.error_cls, code)

    def _ensure_schema(self):
        if self.db.in_transaction:
            self._fail('storage_transaction_busy')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}
        # Explicit BEGIN makes SQLite DDL and user_version one atomic upgrade.
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            if 'media_json' not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'")
            if 'completed_at' not in columns:
                self.db.execute('ALTER TABLE messages ADD COLUMN completed_at INTEGER NOT NULL DEFAULT 0')
            if 'completion_reason' not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN completion_reason TEXT NOT NULL DEFAULT ''")
            self.db.execute('CREATE INDEX IF NOT EXISTS history_scope ON messages(conversation_type,group_id,created,id)')
            self.db.execute('CREATE INDEX IF NOT EXISTS history_completion ON messages(completed_at,created,id)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS history_tombstones(
                source_hash BLOB PRIMARY KEY, content_hash BLOB NOT NULL,
                message_id TEXT NOT NULL, removed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS history_reply_tombstones(
                key_hash BLOB PRIMARY KEY, message_id TEXT NOT NULL, text_hash BLOB NOT NULL,
                state TEXT NOT NULL, removed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)''')
            # Successful historical sends are the only automatically inferred
            # completions.  Webhook receipt and uncertain/not_sent stay pending.
            self.db.execute("""UPDATE messages SET completed_at=MAX(created,1),
                completion_reason='adapter_completed' WHERE completed_at=0 AND id IN
                (SELECT message_id FROM replies WHERE state='adapter_completed')
                AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.message_id=messages.id AND r.state IN ('uncertain','sending'))""")
            self.db.execute("""UPDATE messages SET completed_at=0,completion_reason=''
                WHERE completion_reason!='explicit' AND EXISTS(SELECT 1 FROM replies r
                WHERE r.message_id=messages.id AND r.state IN ('uncertain','sending'))""")
            self._trim_tombstones()
            self.db.execute('PRAGMA user_version=3')

    def _has_table(self, name):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _trim_tombstones(self):
        now = int(self.clock())
        # A joint count bound, not a fresh allowance for each table.
        for table in ('history_tombstones', 'history_reply_tombstones'):
            self.db.execute(f'DELETE FROM {table} WHERE expires_at<=?', (now,))
        rows = self.db.execute('''SELECT kind,rowid FROM (
            SELECT 0 AS kind,rowid,removed_at FROM history_tombstones UNION ALL
            SELECT 1 AS kind,rowid,removed_at FROM history_reply_tombstones)
            ORDER BY removed_at DESC,kind,rowid DESC LIMIT -1 OFFSET ?''', (self.max_tombstones,)).fetchall()
        for kind, rowid in rows:
            table = 'history_tombstones' if kind == 0 else 'history_reply_tombstones'
            self.db.execute(f'DELETE FROM {table} WHERE rowid=?', (rowid,))

    def lookup_duplicate(self, value):
        row = self.db.execute('''SELECT * FROM messages WHERE conversation_type=?
            AND group_id=? AND sender_id=? AND source_id=?''', tuple(_identity(value))).fetchone()
        if row is not None:
            item = dict(row)
            if _digest(_content(item)) != _digest(_content(value)):
                self._fail('source_id_collision')
            return item['id'], False
        row = self.db.execute('''SELECT message_id,content_hash FROM history_tombstones
            WHERE source_hash=? AND expires_at>?''', (_digest(_identity(value)), int(self.clock()))).fetchone()
        if row is not None:
            if bytes(row[1]) != _digest(_content(value)):
                self._fail('source_id_collision')
            return row[0], False
        return None

    def lookup_reply(self, message_id, text, reply_key):
        row = self.db.execute('''SELECT message_id,text_hash,state FROM history_reply_tombstones
            WHERE key_hash=? AND expires_at>?''', (_digest(reply_key), int(self.clock()))).fetchone()
        if row is None:
            return None
        if row[0] != message_id or bytes(row[1]) != _digest(text):
            self._fail('reply_key_conflict')
        return row[2], False

    def _free_bytes(self):
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        page_count = self.db.execute('PRAGMA page_count').fetchone()[0]
        free_count = self.db.execute('PRAGMA freelist_count').fetchone()[0]
        hard_pages = self.db.execute('PRAGMA max_page_count').fetchone()[0]
        return max(0, min(hard_pages, self.max_bytes // page_size) - page_count + free_count) * page_size

    def _pending_reserve(self, exclude=None):
        # Reserve one maximum-size future reply for each pending message that
        # has no durable reply yet, plus fixed completion/diagnostic headroom.
        sql = '''SELECT count(*) FROM messages m WHERE completed_at=0 AND NOT EXISTS
            (SELECT 1 FROM replies r WHERE r.message_id=m.id)'''
        params = ()
        if exclude is not None:
            sql += ' AND m.id!=?'
            params = (exclude,)
        return self.db.execute(sql, params).fetchone()[0] * self.pending_reply_reserve

    def _eligible(self, limit, protected_message_id=None):
        sql = '''SELECT m.* FROM messages m WHERE completed_at>0 AND NOT EXISTS
            (SELECT 1 FROM replies r WHERE r.message_id=m.id AND (r.state='sending' OR
             (r.state='uncertain' AND m.completion_reason!='explicit')))'''
        if self._has_table('event_deliveries'):
            sql += ''' AND NOT EXISTS(SELECT 1 FROM event_deliveries d WHERE
                d.message_id=m.id AND d.status IN ('pending','retry','sending'))'''
        # Attachment extension adds a table with message_id and state.  Active
        # extraction/download work is protected just like callback delivery.
        if self._has_table('message_attachments'):
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(message_attachments)')}
            if 'state' in columns:
                sql += ''' AND NOT EXISTS(SELECT 1 FROM message_attachments a WHERE
                    a.message_id=m.id AND a.state IN ('pending','fetching','extracting','sending'))'''
        params = []
        if protected_message_id is not None:
            sql += ' AND m.id!=?'
            params.append(protected_message_id)
        sql += ' ORDER BY m.created,m.id LIMIT ?'
        return [dict(row) for row in self.db.execute(sql, (*params, limit)).fetchall()]

    def prune(self, *, required_bytes=0, max_messages=128, protected_message_id=None):
        """Prune oldest safe completed records until admission fits.

        Explicit pruning with required_bytes=0 removes at most max_messages.
        Pending records, in-flight sends, and undelivered events remain pinned.
        All child deletes and finite tombstones commit atomically.
        """
        if self.db.in_transaction:
            self._fail('storage_transaction_busy')
        if type(required_bytes) is not int or required_bytes < 0 or type(max_messages) is not int or not 1 <= max_messages <= 1024:
            self._fail('invalid_history_prune')
        total = 0
        while total < max_messages and (required_bytes == 0 or self._free_bytes() < required_bytes):
            rows = self._eligible(min(16, max_messages - total), protected_message_id)
            if not rows:
                break
            now = int(self.clock())
            with self.db:
                self._trim_tombstones()
                saved_replies = []
                for row in rows:
                    saved_replies.extend(dict(r) for r in self.db.execute(
                        'SELECT * FROM replies WHERE message_id=?', (row['id'],)).fetchall())
                    self.db.execute('DELETE FROM replies WHERE message_id=?', (row['id'],))
                    if self._has_table('event_deliveries'):
                        self.db.execute('DELETE FROM event_deliveries WHERE message_id=?', (row['id'],))
                    if self._has_table('message_attachments'):
                        self.db.execute('DELETE FROM message_attachments WHERE message_id=?', (row['id'],))
                    self.db.execute('DELETE FROM messages WHERE id=?', (row['id'],))
                for row in rows:
                    self.db.execute('INSERT OR REPLACE INTO history_tombstones VALUES(?,?,?,?,?)',
                        (_digest(_identity(row)), _digest(_content(row)), row['id'], now, now + self.tombstone_ttl))
                for row in saved_replies:
                    self.db.execute('INSERT OR REPLACE INTO history_reply_tombstones VALUES(?,?,?,?,?,?)',
                        (_digest(row['reply_key']), row['message_id'], _digest(row['text']), row['state'], now, now + self.tombstone_ttl))
                self._trim_tombstones()
            total += len(rows)
        self.pruned_count += total
        return total

    def _reserve(self, requested, *, exclude=None):
        if self.db.in_transaction:
            self._fail('storage_transaction_busy')
        needed = requested + self.control_reserve + self._pending_reserve(exclude)
        while self._free_bytes() < needed:
            if not self.prune(required_bytes=needed, max_messages=1024, protected_message_id=exclude):
                break
        if self._free_bytes() < needed:
            # Tombstones are the last disposable records; they never grow
            # forever at the expense of accepted pending work.
            with self.db:
                self._trim_tombstones()
                for table in ('history_tombstones', 'history_reply_tombstones'):
                    for row in self.db.execute(f'SELECT rowid FROM {table} ORDER BY removed_at,rowid').fetchall():
                        if self._free_bytes() >= needed:
                            break
                        self.db.execute(f'DELETE FROM {table} WHERE rowid=?', (row[0],))
        if self._free_bytes() < needed:
            self._fail('storage_full')
        self.last_error = None

    def admit(self, value, estimated_extra_bytes=0):
        if type(estimated_extra_bytes) is not int or estimated_extra_bytes < 0:
            self._fail('invalid_history_reservation')
        size = len(_json(value).encode('utf-8'))
        # Conservative row/index/page-split allowance. The hard page cap remains
        # authoritative even if an unanticipated shared-table write needs more.
        self._reserve(16 * 1024 + 2 * size + estimated_extra_bytes + self.pending_reply_reserve)

    def admit_reply(self, message_id, text, reply_key=None):
        self._reserve(16 * 1024 + 2 * len(text.encode('utf-8')), exclude=message_id)

    def complete(self, message_id, reason='explicit'):
        if reason not in ('explicit', 'adapter_completed'):
            self._fail('invalid_completion_reason')
        if self.db.in_transaction:
            self._fail('storage_transaction_busy')
        with self.db:
            row = self.db.execute('SELECT completed_at FROM messages WHERE id=?', (message_id,)).fetchone()
            if row is None:
                self._fail('message_not_found')
            if not row[0] or reason == 'explicit':
                self.db.execute('UPDATE messages SET completed_at=?,completion_reason=? WHERE id=?',
                                (row[0] or max(1, int(self.clock())), reason, message_id))
        return {'message_id': message_id, 'completed': True}

    def finish_reply(self, reply_key, state):
        """Call inside Store.finish's transaction, after updating reply state."""
        if state == 'uncertain':
            self.db.execute("UPDATE messages SET completed_at=0,completion_reason='' WHERE id=(SELECT message_id FROM replies WHERE reply_key=?)", (reply_key,))
        if state == 'adapter_completed':
            self.db.execute('''UPDATE messages SET completed_at=?,completion_reason='adapter_completed'
                WHERE completed_at=0 AND id=(SELECT message_id FROM replies WHERE reply_key=?)
                AND NOT EXISTS(SELECT 1 FROM replies r WHERE r.message_id=messages.id AND r.state='uncertain')''',
                (max(1, int(self.clock())), reply_key))

    def _scope(self, conversation_type, group_id):
        if (conversation_type not in ('private', 'group') or not isinstance(group_id, str) or
                (conversation_type == 'private' and group_id != '') or
                (conversation_type == 'group' and not re.fullmatch(r'[1-9][0-9]{0,19}', group_id))):
            self._fail('invalid_history_scope')
        return conversation_type, group_id

    def _encode_cursor(self, value):
        return base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip('=')

    def _decode_cursor(self, cursor, scope):
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            self._fail('invalid_history_cursor')
        try:
            raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'v', 'scope', 'key'} or value['v'] != 1 or value['scope'] != scope:
                raise ValueError()
            return value['key']
        except (ValueError, TypeError, UnicodeError):
            self._fail('invalid_history_cursor')

    def _query_policy(self):
        """Snapshot current authorization before a synchronous, bounded query.

        The fixed SQLite predicate filters before LIMIT and GROUP BY, including
        counts. It never exposes an SQL interface to callers and never writes.
        Store owns policy validation; revoked rows remain durable but invisible.
        """
        policy = self.policy_provider() if self.policy_provider else None
        owner = self.private_sender_id

        def visible(kind, group, sender):
            row = {'conversation_type': kind, 'group_id': group, 'sender_id': sender}
            if policy is not None:
                return bool(policy.authorizes_stored(row))
            return kind != 'private' or owner is None or sender == owner

        self.db.create_function('dot_history_authorized', 3, visible)
        return policy

    def _query_scope(self, conversation_type, group_id, sender_id, policy):
        kind, group = self._scope(conversation_type, group_id)
        if kind == 'group':
            # Groups retain a shared, whole-conversation scope, never a sender
            # subset selected by the caller.
            if sender_id is not None:
                self._fail('invalid_history_scope')
            return kind, group, None
        if policy is not None:
            allowed = policy.private_sender_ids
        elif self.private_sender_id is not None:
            allowed = (self.private_sender_id,)
        else:
            # Compatibility for independent legacy history stores. Production
            # Store supplies the immutable owner even without a policy provider.
            allowed = tuple(row[0] for row in self.db.execute("""SELECT DISTINCT
                sender_id FROM messages WHERE conversation_type='private'
                AND group_id='' ORDER BY sender_id LIMIT 2"""))
        if sender_id is None:
            if len(allowed) > 1:
                self._fail('private_sender_required')
            sender_id = next(iter(allowed), None)
        elif (not isinstance(sender_id, str) or
                not re.fullmatch(r'[1-9][0-9]{0,19}', sender_id) or
                ((policy is not None or self.private_sender_id is not None) and sender_id not in allowed)):
            self._fail('invalid_history_scope')
        return kind, group, sender_id

    def _privacy(self, row):
        if self.privacy_scope is not None:
            return self.privacy_scope(row)
        if row['conversation_type'] == 'group':
            return 'group_shared'
        return ('private_sender_only' if self.private_sender_id is not None and
                row['sender_id'] != self.private_sender_id else 'private_owner_only')

    def _message_rows(self, action, conversation_type, group_id, limit, cursor, sender_id):
        policy = self._query_policy()
        kind, group, sender = self._query_scope(conversation_type, group_id, sender_id, policy)
        maximum = 20 if action == 'pending' else MAX_HISTORY_PAGE
        if type(limit) is not int or not 1 <= limit <= maximum:
            self._fail('invalid_history_limit')
        scope = [action, kind, group] + ([sender] if kind == 'private' else [])
        params = [kind, group]
        columns = ('id,text,created,conversation_type,group_id,sender_id'
                   if action == 'pending' else '*')
        sql = f"""SELECT {columns} FROM messages WHERE conversation_type=? AND group_id=?
            AND dot_history_authorized(conversation_type,group_id,sender_id)"""
        if kind == 'private':
            sql += ' AND sender_id IS ?'
            params.append(sender)
        if action == 'pending':
            sql += ' AND completed_at=0'
        if cursor is not None:
            key = self._decode_cursor(cursor, scope)
            if (not isinstance(key, list) or len(key) != 2 or type(key[0]) is not int or
                    not isinstance(key[1], str) or not 1 <= len(key[1]) <= 128):
                self._fail('invalid_history_cursor')
            sql += ' AND (created<? OR (created=? AND id<?))'
            params.extend((key[0], key[0], key[1]))
        rows = [dict(row) for row in self.db.execute(
            sql + ' ORDER BY created DESC,id DESC LIMIT ?', (*params, limit + 1)).fetchall()]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = self._encode_cursor({'v': 1, 'scope': scope,
                                               'key': [last['created'], last['id']]})
        return rows[:limit], next_cursor

    def page(self, conversation_type, group_id='', limit=50, cursor=None, sender_id=None):
        rows, next_cursor = self._message_rows(
            'messages', conversation_type, group_id, limit, cursor, sender_id)
        result = []
        for row in rows:
            reply_rows = self.db.execute(
                'SELECT reply_key,text,state FROM replies WHERE message_id=? ORDER BY rowid DESC LIMIT 21',
                (row['id'],)).fetchall()
            result.append({'message_id': row['id'], 'text': row['text'], 'timestamp': row['created'],
                'conversation_type': row['conversation_type'], 'group_id': row['group_id'] or None,
                'sender_id': row['sender_id'], 'privacy_scope': self._privacy(row),
                'completed': row['completed_at'] > 0, 'completed_at': row['completed_at'] or None,
                'completion_reason': row['completion_reason'] or None,
                'replies': [display_reply(reply) for reply in reversed(reply_rows[:20])],
                'replies_truncated': len(reply_rows) > 20})
        return {'messages': result, 'next_cursor': next_cursor}

    def pending(self, conversation_type, group_id='', limit=20, cursor=None, sender_id=None):
        rows, next_cursor = self._message_rows(
            'pending', conversation_type, group_id, limit, cursor, sender_id)
        result = [{'message_id': row['id'], 'text_preview': row['text'][:256],
                   'preview_truncated': len(row['text']) > 256, 'timestamp': row['created'],
                   'sender_id': row['sender_id'], 'conversation_type': row['conversation_type'],
                   'group_id': row['group_id'] or None, 'privacy_scope': self._privacy(row)}
                  for row in rows]
        return {'messages': result, 'next_cursor': next_cursor}

    def conversations(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= MAX_CONVERSATION_PAGE:
            self._fail('invalid_history_limit')
        self._query_policy()
        params = []
        where = ''
        scope = ['conversations', 'sender_scoped']
        if cursor is not None:
            key = self._decode_cursor(cursor, scope)
            if (not isinstance(key, list) or len(key) != 3 or
                    key[0] not in ('private', 'group') or not isinstance(key[1], str) or
                    not isinstance(key[2], str) or
                    (key[0] == 'private' and not re.fullmatch(r'[1-9][0-9]{0,19}', key[2])) or
                    (key[0] == 'group' and key[2] != '')):
                self._fail('invalid_history_cursor')
            self._scope(key[0], key[1])
            where = ' WHERE (conversation_type,group_id,conversation_sender) > (?,?,?)'
            params.extend(key)
        rows = [dict(row) for row in self.db.execute("""SELECT * FROM (
            SELECT conversation_type,group_id,
            CASE WHEN conversation_type='private' THEN sender_id ELSE '' END AS conversation_sender,
            count(*) AS message_count,sum(CASE WHEN completed_at=0 THEN 1 ELSE 0 END) AS pending_count,
            max(created) AS last_message_at FROM messages
            WHERE dot_history_authorized(conversation_type,group_id,sender_id)
            GROUP BY conversation_type,group_id,conversation_sender)""" + where + """
            ORDER BY conversation_type,group_id,conversation_sender LIMIT ?""", (*params, limit + 1)).fetchall()]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = self._encode_cursor({'v': 1, 'scope': scope,
                'key': [last['conversation_type'], last['group_id'], last['conversation_sender']]})
        result = rows[:limit]
        for row in result:
            row['sender_id'] = row.pop('conversation_sender') or None
        return {'conversations': result, 'next_cursor': next_cursor}

    def status(self):
        mode = self.db.execute('PRAGMA journal_mode').fetchone()[0]
        db_bytes, wal_bytes = _file_bytes(self.path), _file_bytes(str(self.path) + '-wal')
        return {'limit_bytes': self.max_bytes, 'limit_mib': self.max_bytes / (1024 * 1024),
            'database_bytes': db_bytes, 'wal_bytes': wal_bytes, 'used_bytes': db_bytes + wal_bytes,
            'journal_mode': mode, 'includes': 'database_and_wal',
            'temporary_journal_excluded': True, 'free_page_bytes': self._free_bytes(),
            'pending_messages': self.db.execute('SELECT count(*) FROM messages WHERE completed_at=0').fetchone()[0],
            'completed_messages': self.db.execute('SELECT count(*) FROM messages WHERE completed_at>0').fetchone()[0],
            'tombstones': self.db.execute('SELECT (SELECT count(*) FROM history_tombstones)+(SELECT count(*) FROM history_reply_tombstones)').fetchone()[0],
            'max_tombstones': self.max_tombstones, 'tombstone_ttl_seconds': self.tombstone_ttl,
            'pruned_this_run': self.pruned_count, 'rejected_this_run': self.rejected_count,
            'blocked': self.last_error in ('storage_full', 'storage_busy', 'storage_failed'),
            'last_error': self.last_error}
