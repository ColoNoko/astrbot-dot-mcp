"""Host-owned binding, durable inbox and fail-closed reply authorization."""
import asyncio
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

MAX_TEXT = 16384

class Rejected(ValueError):
    pass

class NotSent(Rejected):
    """Definitive rejection before entering any adapter send."""
    pass

@dataclass(frozen=True)
class Binding:
    platform_id: str
    adapter: str
    self_id: str
    sender_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise Rejected('invalid_binding')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v) for v in (self.self_id, self.sender_id)):
            raise Rejected('invalid_binding')
        if self.sender_id == self.self_id:
            raise Rejected('invalid_binding')


def valid_text(text):
    if not isinstance(text, str) or not text.strip():
        raise Rejected('invalid_text')
    try:
        if len(text.encode('utf-8')) > MAX_TEXT:
            raise Rejected('text_too_large')
    except UnicodeError:
        raise Rejected('invalid_text') from None
    return text


def snapshot(event, binding, plain_type, at_type=None, group_ids=(), group_mode='owner_at'):
    """Bound private owner; explicit group mode gates accepted group senders."""
    try:
        if group_mode not in ('owner_at', 'all_messages'):
            raise Rejected('invalid_group_trigger_mode')
        raw_text = None
        raw_source_id = None
        expected_normalized = None
        raw_self, raw_sender = event.get_self_id(), event.get_sender_id()
        if any(type(v) not in (str, int) for v in (raw_self, raw_sender)):
            raise Rejected('invalid_identifiers')
        if (event.get_platform_id() != binding.platform_id or
                event.get_platform_name() != binding.adapter or
                str(raw_self) != binding.self_id):
            raise Rejected('not_bound_message')
        private, observed_group = event.is_private_chat(), event.get_group_id()
        if private is True and observed_group in ('', None):
            kind, group_id = 'private', ''
        elif (private is False and type(observed_group) in (str, int) and
              str(observed_group) in group_ids and binding.adapter == 'aiocqhttp'):
            kind, group_id = 'group', str(observed_group)
        else:
            raise Rejected('not_bound_conversation')
        sender_id = str(raw_sender)
        if kind == 'private' or group_mode == 'owner_at':
            if sender_id != binding.sender_id:
                raise Rejected('not_bound_sender')
        elif not re.fullmatch(r'[1-9][0-9]{0,19}', sender_id):
            raise Rejected('invalid_identifiers')
        # Never process an outbound echo, including explicit all-sender mode.
        if sender_id == binding.self_id:
            raise Rejected('bot_self_message')
        if binding.adapter == 'aiocqhttp':
            raw = event.message_obj.raw_message
            if (not isinstance(raw, dict) or raw.get('post_type') != 'message' or
                    raw.get('message_type') != kind or
                    any(type(raw.get(k)) not in (str, int) for k in ('self_id', 'user_id')) or
                    str(raw['self_id']) != binding.self_id or str(raw['user_id']) != sender_id):
                raise Rejected('raw_route_mismatch')
            sender = raw.get('sender')
            if (not isinstance(sender, dict) or type(sender.get('user_id')) not in (str, int) or
                    str(sender['user_id']) != sender_id):
                raise Rejected('raw_sender_mismatch')
            if kind == 'group':
                if type(raw.get('group_id')) not in (str, int) or str(raw['group_id']) != group_id:
                    raise Rejected('raw_route_mismatch')
            elif raw.get('group_id') not in (None, ''):
                raise Rejected('raw_route_mismatch')
            raw_segments = raw.get('message')
            if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 256:
                raise Rejected('raw_text_only')
            texts, normalized_runs, run, mentions = [], [], [], 0
            for segment in raw_segments:
                if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
                    raise Rejected('raw_text_only')
                data = segment['data']
                if segment.get('type') == 'text' and isinstance(data.get('text'), str):
                    texts.append(data['text']); run.append(data['text'])
                elif (kind == 'group' and segment.get('type') == 'at' and
                      type(data.get('qq')) in (str, int) and str(data['qq']) == binding.self_id):
                    normalized_runs.append(''.join(run).strip()); run = []
                    mentions += 1
                else:
                    raise Rejected('raw_text_only')
            normalized_runs.append(''.join(run).strip())
            if kind == 'group' and group_mode == 'owner_at' and not mentions:
                raise Rejected('bot_mention_required')
            raw_text = valid_text(''.join(texts))
            expected_normalized = ''.join(normalized_runs)
            raw_source_id = raw.get('message_id')
            if type(raw_source_id) not in (str, int):
                raise Rejected('raw_message_id_mismatch')
        source_id, umo, segments = event.message_obj.message_id, event.unified_msg_origin, event.get_messages()
        if type(source_id) not in (str, int):
            raise Rejected('invalid_message_id')
        source_id = str(source_id)
        if raw_source_id is not None and str(raw_source_id) != source_id:
            raise Rejected('raw_message_id_mismatch')
        if any(not isinstance(v, str) or not v.strip() or any(ord(c) < 32 or ord(c) == 127 for c in v)
               for v in (source_id, umo)) or len(source_id) > 256 or len(umo) > 1024:
            raise Rejected('invalid_message_identity')
        if not isinstance(segments, list) or not 1 <= len(segments) <= 256:
            raise Rejected('text_only')
        normalized = []
        for segment in segments:
            if type(segment) is plain_type and isinstance(segment.text, str):
                normalized.append(segment.text)
            elif (kind == 'group' and at_type is not None and type(segment) is at_type and
                  type(segment.qq) in (str, int) and str(segment.qq) == binding.self_id):
                pass
            else:
                raise Rejected('text_only')
        normalized_text = valid_text(''.join(normalized))
        if expected_normalized is not None and normalized_text != expected_normalized:
            raise Rejected('text_normalization_mismatch')
        return {'source_id': source_id, 'umo': umo,
                'text': raw_text if raw_text is not None else normalized_text,
                'conversation_type': kind, 'group_id': group_id, 'sender_id': sender_id}
    except Rejected:
        raise
    except Exception:
        raise Rejected('invalid_event') from None

class Store:
    def __init__(self, path, binding):
        self.binding = binding
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise Rejected('unsafe_database_path')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        try:
            os.chmod(path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('CREATE TABLE IF NOT EXISTS binding(value TEXT NOT NULL)')
            identity = json.dumps(asdict(binding), sort_keys=True)
            existing = self.db.execute('SELECT value FROM binding').fetchone()
            if existing and existing[0] != identity:
                raise Rejected('binding_changed_requires_new_database')
            with self.db:
                if existing is None:
                    self.db.execute('INSERT INTO binding VALUES (?)', (identity,))
            self._ensure_schema()
            with self.db:
                self.db.execute("UPDATE replies SET state='uncertain' WHERE state='sending'")
        except BaseException:
            self.db.close()
            raise
        self.events = None

    def _create_tables(self, suffix=''):
        # suffix is an internal migration constant, never external input.
        self.db.execute(f'''CREATE TABLE messages{suffix}(
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL, umo TEXT NOT NULL,
            text TEXT NOT NULL, created INTEGER NOT NULL,
            conversation_type TEXT NOT NULL CHECK(conversation_type IN ('private','group')),
            group_id TEXT NOT NULL, sender_id TEXT NOT NULL,
            UNIQUE(conversation_type,group_id,sender_id,source_id))''')
        self.db.execute(f'''CREATE TABLE replies{suffix}(reply_key TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages{suffix}(id),
            text TEXT NOT NULL, state TEXT NOT NULL)''')

    def _ensure_schema(self):
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(messages)')}
        old = {'id', 'source_id', 'umo', 'text', 'created'}
        latest = old | {'conversation_type', 'group_id', 'sender_id'}
        if not columns:
            self.db.execute('BEGIN IMMEDIATE')
            with self.db:
                self._create_tables()
                self.db.execute('PRAGMA user_version=2')
            return
        if columns == latest:
            return
        if columns != old:
            raise Rejected('unsupported_database_schema')
        if {row['name'] for row in self.db.execute('PRAGMA table_info(replies)')} != {
                'reply_key', 'message_id', 'text', 'state'}:
            raise Rejected('unsupported_database_schema')
        # Rebuild the old UNIQUE(source_id) table atomically. Existing opaque
        # IDs, reply keys and event-delivery references are preserved.
        self.db.execute('PRAGMA foreign_keys=OFF')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self._create_tables('_v2')
            self.db.execute('''INSERT INTO messages_v2
                SELECT id,source_id,umo,text,created,'private','',? FROM messages''',
                (self.binding.sender_id,))
            self.db.execute('INSERT INTO replies_v2 SELECT * FROM replies')
            self.db.execute('DROP TABLE replies')
            self.db.execute('DROP TABLE messages')
            self.db.execute('ALTER TABLE messages_v2 RENAME TO messages')
            self.db.execute('ALTER TABLE replies_v2 RENAME TO replies')
            if self.db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise Rejected('database_migration_failed')
            self.db.execute('PRAGMA user_version=2')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.db.execute('PRAGMA foreign_keys=ON')

    def ingest(self, value):
        kind = value.get('conversation_type', 'private')
        group = value.get('group_id', '')
        sender = value.get('sender_id', self.binding.sender_id)
        if (kind not in ('private', 'group') or not isinstance(sender, str) or
                sender == self.binding.self_id or not isinstance(group, str) or
                (kind == 'private' and (group != '' or sender != self.binding.sender_id)) or
                (kind == 'group' and (not re.fullmatch(r'[1-9][0-9]{0,19}', group) or
                                      not re.fullmatch(r'[1-9][0-9]{0,19}', sender)))):
            raise Rejected('invalid_conversation')
        with self.db:
            row = self.db.execute('''SELECT * FROM messages WHERE conversation_type=?
                AND group_id=? AND sender_id=? AND source_id=?''',
                (kind, group, sender, value['source_id'])).fetchone()
            if row:
                if (row['umo'], row['text']) != (value['umo'], value['text']):
                    raise Rejected('source_id_collision')
                return row['id'], False
            mid, now = 'msg_' + uuid.uuid4().hex, int(time.time())
            self.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)',
                            (mid, value['source_id'], value['umo'], value['text'], now, kind, group, sender))
            if self.events:
                self.events.enqueue(mid, now)
        return mid, True

    def message(self, mid):
        if not isinstance(mid, str):
            raise Rejected('invalid_message_id')
        row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if row is None:
            raise Rejected('message_not_found')
        return dict(row)

    def read(self, message_id):
        row = self.message(message_id)
        return {'message_id': row['id'], 'text': row['text'], 'timestamp': row['created'],
                'conversation_type': row['conversation_type'],
                'group_id': row['group_id'] if row['conversation_type'] == 'group' else None,
                'sender_id': row['sender_id'],
                'privacy_scope': 'group_shared' if row['conversation_type'] == 'group' else 'private_owner_only'}

    def claim(self, message_id, text, reply_key):
        self.message(message_id)
        valid_text(text)
        if not isinstance(reply_key, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', reply_key):
            raise Rejected('invalid_reply_key')
        with self.db:
            row = self.db.execute('SELECT * FROM replies WHERE reply_key=?', (reply_key,)).fetchone()
            if row:
                if (row['message_id'], row['text']) != (message_id, text):
                    raise Rejected('reply_key_conflict')
                return row['state'], False
            self.db.execute('INSERT INTO replies VALUES (?,?,?,?)', (reply_key, message_id, text, 'sending'))
        return 'sending', True

    def finish(self, key, state):
        with self.db:
            self.db.execute('UPDATE replies SET state=? WHERE reply_key=?', (state, key))

    def status(self):
        return {'text_only': True, 'messages': self.db.execute('SELECT count(*) FROM messages').fetchone()[0],
                'replies': dict(self.db.execute('SELECT state,count(*) FROM replies GROUP BY state'))}

    def close(self):
        self.db.close()


class Host:
    """Only the host executes sends; helpers cannot choose a destination."""
    def __init__(self, store, preflight, send, timeout=10):
        self.store, self.preflight, self.send = store, preflight, send
        self.timeout = timeout
        self.stopping = False
        self.tasks = set()
        self.revoked = set()

    async def reply(self, message_id, text, reply_key):
        if self.stopping:
            raise Rejected('host_stopping')
        state, fresh = self.store.claim(message_id, text, reply_key)
        if not fresh:
            return {'reply_key': reply_key, 'state': state}
        row = self.store.message(message_id)
        deadline = asyncio.get_running_loop().time() + self.timeout
        send_started = False
        async def run():
            nonlocal send_started
            try:
                # A platform lookup may await; recheck revocation AFTER it.
                try:
                    target = await self.preflight(row)
                except Exception:
                    return 'not_sent'
                if (self.stopping or reply_key in self.revoked or
                        asyncio.get_running_loop().time() >= deadline):
                    return 'not_sent'
                send_started = True
                result = await self.send(target, row, text)
                # AstrBot accepting a send is not proof QQ delivered it.
                return 'adapter_completed' if result is True else 'uncertain'
            except asyncio.CancelledError:
                raise
            except NotSent:
                return 'not_sent'
            except Exception:
                return 'uncertain'
        task = asyncio.create_task(run())
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            self.revoked.discard(reply_key)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout)
            if task in done:
                state = task.result()
            else:
                self.revoked.add(reply_key)
                task.cancel()
                state = 'uncertain' if send_started else 'not_sent'
        except asyncio.CancelledError:
            self.revoked.add(reply_key)
            task.cancel()
            self.store.finish(reply_key, 'uncertain' if send_started else 'not_sent')
            raise
        finally:
            # No raw adapter errors, payloads or secrets are emitted.
            pass
        self.store.finish(reply_key, state)
        return {'reply_key': reply_key, 'state': state}

    async def close(self):
        self.stopping = True
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
