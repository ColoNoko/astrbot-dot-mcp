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
import unicodedata
if __package__:
    from .history_store import configure_storage, BoundedHistory, storage_error
    from .message_timing import MessageTiming
    from .processing_watch import ProcessingWatch
    from .service_notice import file_service_notice, has_file
    from .access_policy import AccessPolicy, PolicyError
else:  # Standalone synthetic host tests.
    from history_store import configure_storage, BoundedHistory, storage_error
    from message_timing import MessageTiming
    from processing_watch import ProcessingWatch
    from service_notice import file_service_notice, has_file
    from access_policy import AccessPolicy, PolicyError

MAX_TEXT = 16384
MEDIA_REPLY_PREFIX = "[dot-media-v1]:"

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


def file_display_name(value):
    """OneBot data.file as display-only basename, never a path or source grant."""
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return None
    normalized = unicodedata.normalize('NFKC', value)
    if (not normalized.strip() or normalized in ('.', '..') or
            any(c in normalized for c in '/\\:') or
            any(unicodedata.category(c).startswith('C') for c in normalized)):
        return None
    return normalized


def safe_image_file_id(value):
    """Known hash-shaped image IDs only; ordinary basenames may name local files."""
    return (isinstance(value, str) and
            re.fullmatch(r'[A-Fa-f0-9]{32}\.(?:image|jpg|jpeg|png|gif|webp|bmp)',
                         value, flags=re.IGNORECASE) is not None)


def snapshot(event, binding, plain_type, at_type=None, group_ids=(), group_mode='owner_at', media_types=None,
             component_types=None, policy=None, allow_unmentioned_group_file=False):
    """Validate one raw/normalized message against the current conversation policy."""
    try:
        if policy is not None:
            if not isinstance(policy, AccessPolicy) or policy.binding != binding:
                raise Rejected('invalid_access_policy')
        elif group_mode not in ('owner_at', 'all_messages'):
            raise Rejected('invalid_group_trigger_mode')
        media_types = media_types or {}
        component_types = component_types or {}
        attachments = []
        raw_media_counts = {'image': 0, 'file': 0}
        raw_component_counts, raw_mentions = {}, {}
        reply_source_ids, intent_texts, mentions = [], [], 0
        has_placeholders = False
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
              (policy.group_rule(str(observed_group)) is not None if policy is not None
               else str(observed_group) in group_ids) and binding.adapter == 'aiocqhttp'):
            kind, group_id = 'group', str(observed_group)
        else:
            raise Rejected('not_bound_conversation')
        sender_id = str(raw_sender)
        if policy is not None:
            if not policy.authorizes_stored({'conversation_type': kind, 'group_id': group_id,
                                             'sender_id': sender_id}):
                raise Rejected('not_bound_sender')
        elif kind == 'private' or group_mode == 'owner_at':
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
            for segment_index, segment in enumerate(raw_segments):
                if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
                    raise Rejected('raw_text_only')
                data, segment_type = segment['data'], segment.get('type')
                if (not isinstance(segment_type, str) or not 1 <= len(segment_type) <= 64 or
                        any(ord(c) < 32 or ord(c) == 127 for c in segment_type)):
                    raise Rejected('raw_text_only')
                if segment_type == 'text':
                    if not isinstance(data.get('text'), str):
                        raise Rejected('raw_text_only')
                    texts.append(data['text']); run.append(data['text']); intent_texts.append(data['text'])
                    continue
                # AstrBot 4.28.2 strips every consecutive text run, including
                # runs separated by a component that the adapter later drops.
                normalized_runs.append(''.join(run).strip()); run = []
                if segment_type == 'at':
                    target = data.get('qq')
                    if (type(target) not in (str, int) or
                            (str(target) != 'all' and not re.fullmatch(r'[1-9][0-9]{0,19}', str(target)))):
                        raise Rejected('raw_text_only')
                    target = str(target)
                    raw_mentions[target] = raw_mentions.get(target, 0) + 1
                    if kind == 'group' and target == binding.self_id:
                        mentions += 1
                    else:
                        texts.append('[提及全体成员]' if target == 'all' else '[提及成员]')
                        has_placeholders = True
                elif segment_type in media_types:
                    kind_media = segment_type
                    if len(attachments) >= 8:
                        raise Rejected('too_many_attachments')
                    name = (data.get('file_name') or data.get('name') or
                            (file_display_name(data.get('file')) if kind_media == 'file' else None) or
                            ('image' if kind_media == 'image' else 'file'))
                    if not isinstance(name, str) or len(name) > 256:
                        raise Rejected('invalid_attachment')
                    url = data.get('url')
                    if url is None and isinstance(data.get('file'), str) and data['file'].startswith(('http://', 'https://')):
                        url = data['file']
                    file_id = data.get('file_id') if kind_media == 'file' else None
                    if kind_media == 'image' and safe_image_file_id(data.get('file')):
                        file_id = data['file']
                    if url is not None and (not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url)):
                        raise Rejected('invalid_attachment')
                    if file_id is not None and (type(file_id) not in (str, int) or not str(file_id) or len(str(file_id)) > 256):
                        raise Rejected('invalid_attachment')
                    attachments.append({'segment_index': segment_index, 'kind': kind_media,
                                        'filename': name, 'url': url or '', 'file_id': str(file_id) if file_id is not None else ''})
                    raw_media_counts[kind_media] += 1
                else:
                    if segment_type == 'reply':
                        reply_id = data.get('id')
                        if (type(reply_id) in (str, int) and 0 < len(str(reply_id)) <= 256 and
                                not any(ord(c) < 32 or ord(c) == 127 for c in str(reply_id))):
                            reply_source_ids.append(str(reply_id))
                    texts.append({'reply': '[引用消息]', 'face': '[表情]'}.get(
                        segment_type, '[暂不支持的消息]'))
                    has_placeholders = True
                    # Only these adapter branches produce Plain from non-text
                    # raw data. Validate their normalization, never expose it.
                    if segment_type in ('markdown', 'plain'):
                        value = ((data.get('markdown') or data.get('content', ''))
                                 if segment_type == 'markdown' else data.get('text'))
                        if not isinstance(value, str):
                            raise Rejected('raw_text_only')
                        normalized_runs.append(value)
                    else:
                        cls = component_types.get(segment_type)
                        if isinstance(cls, type) and cls not in (plain_type, at_type):
                            raw_component_counts[cls] = raw_component_counts.get(cls, 0) + 1
            normalized_runs.append(''.join(run).strip())
            if (kind == 'group' and not mentions and
                    (policy.group_rule(group_id).mode == 'at' if policy is not None
                     else group_mode == 'owner_at') and not (allow_unmentioned_group_file is True and
                    any(item['kind'] == 'file' for item in attachments))):
                raise Rejected('bot_mention_required')
            raw_text = ''.join(texts)
            if raw_text.strip() or not attachments:
                valid_text(raw_text)
            elif len(raw_text.encode('utf-8')) > MAX_TEXT:
                raise Rejected('text_too_large')
            expected_normalized = ''.join(normalized_runs)
            if len(expected_normalized.encode('utf-8')) > MAX_TEXT:
                raise Rejected('text_too_large')
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
        if not isinstance(segments, list) or not (0 if attachments or has_placeholders else 1) <= len(segments) <= 256:
            raise Rejected('text_only')
        normalized = []
        observed_media = {'image': 0, 'file': 0}
        for segment in segments:
            if type(segment) is plain_type and isinstance(segment.text, str):
                normalized.append(segment.text)
            elif at_type is not None and type(segment) is at_type:
                if type(segment.qq) not in (str, int) or raw_mentions.get(str(segment.qq), 0) <= 0:
                    raise Rejected('mention_normalization_mismatch')
                target = str(segment.qq)
                raw_mentions[target] -= 1
            elif any(type(segment) is cls for cls in media_types.values()):
                media_kind = next(k for k, cls in media_types.items() if type(segment) is cls)
                observed_media[media_kind] += 1
                if observed_media[media_kind] > raw_media_counts[media_kind]:
                    raise Rejected('media_normalization_mismatch')
            else:
                cls = type(segment)
                if raw_component_counts.get(cls, 0) <= 0:
                    raise Rejected('text_only')
                # No component attributes/serializers: Reply.chain and other
                # nested payloads are outside this message's authorized body.
                raw_component_counts[cls] -= 1
        normalized_text = ''.join(normalized)
        if expected_normalized is None and (normalized_text.strip() or not attachments):
            valid_text(normalized_text)
        if expected_normalized is not None and normalized_text != expected_normalized:
            raise Rejected('text_normalization_mismatch')
        return {'source_id': source_id, 'umo': umo,
                'text': raw_text if raw_text is not None else normalized_text,
                'conversation_type': kind, 'group_id': group_id, 'sender_id': sender_id,
                'attachments': attachments,
                'intent_text': ''.join(intent_texts) if binding.adapter == 'aiocqhttp' else normalized_text,
                'reply_source_ids': list(dict.fromkeys(reply_source_ids)),
                'trigger_evidence': {
                    'configured_group_mode': (policy.group_rule(group_id).mode if policy is not None
                        else 'at' if group_mode == 'owner_at' else 'all') if kind == 'group' else None,
                    'top_level_bot_mention': bool(mentions) if binding.adapter == 'aiocqhttp' else None,
                    'trigger_matched': kind != 'group' or bool(mentions) or
                        (policy.group_rule(group_id).mode != 'at' if policy is not None else group_mode != 'owner_at')}}
    except Rejected:
        raise
    except Exception:
        raise Rejected('invalid_event') from None

class Store:
    def __init__(self, path, binding, *, policy_provider=None):
        self.binding = binding
        # Main owns this callable; message/tool payloads cannot choose recipients.
        self.policy_provider = policy_provider
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink():
            raise Rejected('unsafe_database_path')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.attachments = None
        self.db = sqlite3.connect(path)
        try:
            os.chmod(path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('CREATE TABLE IF NOT EXISTS binding(value TEXT NOT NULL)')
            identity = json.dumps(asdict(binding), sort_keys=True)
            existing = self.db.execute('SELECT value FROM binding').fetchone()
            if existing and existing[0] != identity:
                raise Rejected('binding_changed_requires_new_database')
            configure_storage(self.db, path, error_cls=Rejected)
            with self.db:
                if existing is None:
                    self.db.execute('INSERT INTO binding VALUES (?)', (identity,))
            self._ensure_schema()
            self._ensure_evidence_schema()
            self.history = BoundedHistory(self.db, path, error_cls=Rejected,
                policy_provider=self.current_policy, privacy_scope=self.privacy_scope,
                private_sender_id=binding.sender_id)
            with self.db:
                self.db.execute("UPDATE replies SET state='uncertain' WHERE state='sending'")
            self.processing_watch = ProcessingWatch(self.db)
        except BaseException:
            self.db.close()
            raise
        self.events = None
        self.reminders_enabled = lambda: False
        self.message_timing = MessageTiming()

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
        if columns == latest or (latest <= columns <= latest | {'media_json', 'completed_at', 'completion_reason', 'trigger_evidence_json', 'linked_media_json', 'group_file_context_json'}):
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

    def _ensure_evidence_schema(self):
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(messages)')}
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            for name, default in (('trigger_evidence_json', '{}'), ('linked_media_json', '[]'),
                                  ('group_file_context_json', '{}')):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE messages ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")

    def ingest(self, value, *, captured_at=None, silent=False, reminder_eligible=None):
        ingest_at = (time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000)
        kind = value.get('conversation_type', 'private')
        group = value.get('group_id', '')
        sender = value.get('sender_id', self.binding.sender_id)
        policy = self.current_policy()
        if (kind not in ('private', 'group') or not isinstance(sender, str) or
                sender == self.binding.self_id or not isinstance(group, str) or
                (kind == 'private' and (group != '' or
                    (sender not in policy.private_sender_ids if policy is not None
                     else sender != self.binding.sender_id))) or
                (kind == 'group' and (not re.fullmatch(r'[1-9][0-9]{0,19}', group) or
                                      not re.fullmatch(r'[1-9][0-9]{0,19}', sender)))):
            raise Rejected('invalid_conversation')
        value = {**value, 'conversation_type': kind, 'group_id': group, 'sender_id': sender}
        if policy is not None and not policy.authorizes_stored(value):
            raise Rejected('invalid_conversation')
        value['media_json'] = json.dumps(value.get('attachments', []), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        duplicate = self.history.lookup_duplicate(value)
        if duplicate:
            return duplicate
        self.history.admit(value, estimated_extra_bytes=(len(value['media_json'].encode('utf-8')) * 2 +
            len(json.dumps(value.get('linked_attachments', []), ensure_ascii=False).encode('utf-8')) * 2 +
            len(json.dumps(value.get('group_file_context', {}), ensure_ascii=False).encode('utf-8')) + 5120))
        events, queued, enqueued_at = (None if silent else self.events), 0, None
        evidence = value.get('trigger_evidence', {})
        evidence = {key: evidence.get(key) for key in
            ('configured_group_mode', 'top_level_bot_mention', 'trigger_matched')} if evidence else {}
        if (evidence and (evidence['configured_group_mode'] not in (None, 'at', 'all', 'selected') or
                any(evidence[key] is not None and type(evidence[key]) is not bool for key in
                    ('top_level_bot_mention', 'trigger_matched')))):
            raise Rejected('invalid_trigger_evidence')
        linked = value.get('linked_attachments', [])
        context = value.get('group_file_context', {})
        links = value.get('candidate_links', [])
        if not isinstance(linked, list) or len(linked) > 8 or not isinstance(links, list) or len(links) > 8:
            raise Rejected('invalid_candidate_links')
        context_json = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
        links_json = json.dumps(links, ensure_ascii=False, separators=(',', ':'))
        if len(context_json.encode('utf-8')) + len(links_json.encode('utf-8')) > 32768:
            raise Rejected('invalid_candidate_links')
        try:
            with self.db:
                mid, now = 'msg_' + uuid.uuid4().hex, int(time.time())
                self.db.execute('INSERT INTO messages(id,source_id,umo,text,created,conversation_type,group_id,sender_id,media_json,trigger_evidence_json,linked_media_json,group_file_context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (mid, value['source_id'], value['umo'], value['text'], now, kind, group, sender, value['media_json'], json.dumps(evidence), links_json, context_json))
                self.processing_watch.enroll(mid, eligible=(self.reminders_enabled() is True and
                    reminder_eligible is not False))
                if self.attachments:
                    self.attachments.register(mid, value.get('attachments', []) + linked)
                if events:
                    queued = events.enqueue(mid, now, captured_at=captured_at, ingest_at=ingest_at,
                        **({"file_request": True} if has_file(value.get("attachments", []) + linked) else {}))
                    if queued:
                        enqueued_at = self.message_timing.stamp()
        except sqlite3.Error as error:
            raise Rejected(self.history.note_storage_error(error)) from None
        # In-memory diagnostics admit only committed messages. A duplicate or
        # rejected/rolled-back payload cannot create or replace a timing record.
        self.message_timing.note_ingest(mid, captured_at=captured_at,
                                       ingest_at=ingest_at, enqueued_at=enqueued_at)
        # Wake only after the message and its deliveries have committed together.
        if events and queued:
            events.notify_enqueued()
        return mid, True

    def message(self, mid):
        if not isinstance(mid, str):
            raise Rejected('invalid_message_id')
        row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if row is None:
            raise Rejected('message_not_found')
        return dict(row)

    def current_policy(self):
        if self.policy_provider is None:
            return None
        try:
            policy = self.policy_provider()
        except (PolicyError, ValueError):
            raise Rejected('binding_inactive') from None
        if not isinstance(policy, AccessPolicy) or policy.binding != self.binding:
            raise Rejected('binding_inactive')
        return policy

    def privacy_scope(self, row):
        if row['conversation_type'] == 'group':
            return 'group_shared'
        return ('private_owner_only' if row['sender_id'] == self.binding.sender_id
                else 'private_sender_only')

    def read(self, message_id):
        row = self.message(message_id)
        policy = self.current_policy()
        if policy is not None and not policy.authorizes_stored(row):
            raise Rejected('binding_inactive')
        context = json.loads(row.get('group_file_context_json', '{}'))
        provider = getattr(self, 'read_context_provider', None)
        if callable(provider) and json.loads(row.get('linked_media_json', '[]')):
            context = provider(row)
        attachments = self.attachments.list(message_id) if self.attachments else []
        if context.get('linked_attachment_state') in ('expired', 'unavailable'):
            indices = {item['segment_index'] for item in json.loads(row.get('linked_media_json', '[]'))}
            attachments = [item for item in attachments if self.attachments.descriptor(
                message_id, item['attachment_id'])['segment_index'] not in indices]
        return {'message_id': row['id'], 'text': row['text'], 'timestamp': row['created'],
                'conversation_type': row['conversation_type'],
                'group_id': row['group_id'] if row['conversation_type'] == 'group' else None,
                'sender_id': row['sender_id'],
                'privacy_scope': self.privacy_scope(row),
                'processing_state': 'completed' if row.get('completed_at') else 'pending',
                'attachments': attachments,
                **({'service_notice': file_service_notice(row['id'])} if has_file(attachments) else {}),
                'trigger_evidence': json.loads(row.get('trigger_evidence_json', '{}')) or {
                    'configured_group_mode': None, 'top_level_bot_mention': None, 'trigger_matched': None},
                **({'group_file_context': context} if context else {})}

    def claim(self, message_id, text, reply_key):
        valid_text(text)
        if not isinstance(reply_key, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', reply_key):
            raise Rejected('invalid_reply_key')
        previous = self.history.lookup_reply(message_id, text, reply_key)
        if previous is not None:
            return previous
        self.message(message_id)
        live = self.db.execute('SELECT message_id,text,state FROM replies WHERE reply_key=?', (reply_key,)).fetchone()
        if live:
            if (live['message_id'], live['text']) != (message_id, text):
                raise Rejected('reply_key_conflict')
            return live['state'], False
        self.history.admit_reply(message_id, text, reply_key)
        with self.db:
            row = self.db.execute('SELECT * FROM replies WHERE reply_key=?', (reply_key,)).fetchone()
            if row:
                if (row['message_id'], row['text']) != (message_id, text):
                    raise Rejected('reply_key_conflict')
                return row['state'], False
            self.db.execute("UPDATE messages SET completed_at=0,completion_reason='' WHERE id=?", (message_id,))
            self.db.execute('INSERT INTO replies VALUES (?,?,?,?)', (reply_key, message_id, text, 'sending'))
        return 'sending', True

    def finish(self, key, state):
        with self.db:
            self.db.execute('UPDATE replies SET state=? WHERE reply_key=?', (state, key))
            self.history.finish_reply(key, state)

    def status(self):
        return {'text_only': self.attachments is None, 'storage': self.history.status(), 'messages': self.db.execute('SELECT count(*) FROM messages').fetchone()[0],
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

    async def reply(self, message_id, text, reply_key, *, _send_override=None):
        if self.stopping:
            raise Rejected('host_stopping')
        if _send_override is None and isinstance(text, str) and text.startswith(MEDIA_REPLY_PREFIX):
            raise Rejected('invalid_text')
        state, fresh = self.store.claim(message_id, text, reply_key)
        if not fresh:
            return {'reply_key': reply_key, 'state': state}
        row = self.store.message(message_id)
        timing = self.store.message_timing
        timing_token = timing.begin_reply(message_id)
        deadline = asyncio.get_running_loop().time() + self.timeout
        send_started = False
        async def run():
            nonlocal send_started
            try:
                # A platform lookup may await; recheck revocation AFTER it.
                try:
                    timing.note_reply_stage(message_id, timing_token, 'preflight_started')
                    target = await self.preflight(row)
                except Exception:
                    return 'not_sent'
                finally:
                    timing.note_reply_stage(message_id, timing_token, 'preflight_finished')
                if (self.stopping or reply_key in self.revoked or
                        asyncio.get_running_loop().time() >= deadline):
                    return 'not_sent'
                send_started = True
                timing.note_reply_stage(message_id, timing_token, 'adapter_started')
                try:
                    result = await (_send_override or self.send)(target, row, text)
                finally:
                    timing.note_reply_stage(message_id, timing_token, 'adapter_finished')
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
            state = 'uncertain' if send_started else 'not_sent'
            timing.finish_reply(message_id, timing_token, state)
            try:
                self.store.finish(reply_key, state)
            except sqlite3.Error as error:
                self.store.history.note_storage_error(error)
            raise
        finally:
            # No raw adapter errors, payloads or secrets are emitted.
            pass
        timing.finish_reply(message_id, timing_token, state)
        try:
            self.store.finish(reply_key, state)
        except sqlite3.Error as error:
            reason = self.store.history.note_storage_error(error)
            timing.finish_reply(message_id, timing_token, 'uncertain')
            # The durable pre-send claim remains sending; never offer a retry
            # after the adapter may have acted but result persistence failed.
            return {'reply_key': reply_key, 'state': 'uncertain', 'storage_error': reason}
        return {'reply_key': reply_key, 'state': state}

    async def close(self):
        self.stopping = True
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
