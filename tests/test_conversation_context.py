"""Synthetic, read-only context tests. No adapter, download, or external calls."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from conversation_context import (build_conversation_context, validate_context_count,
                                  ConversationContextError, MAX_CONTEXT_CHARACTERS)
from host import Binding, Rejected, Store


BINDING = Binding('instance-1', 'aiocqhttp', '100', '200')


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'context.sqlite3'
        self.store = Store(self.path, BINDING)
        self.n = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, text='hello', *, group=None, sender='200', attachments=None):
        self.n += 1
        return self.store.ingest({
            'source_id': str(self.n), 'umo': 'SECRET_SOURCE_UMO', 'text': text,
            'conversation_type': 'private' if group is None else 'group',
            'group_id': '' if group is None else group, 'sender_id': sender,
            'attachments': [] if attachments is None else attachments,
        })[0]

    def reply(self, message_id, text='reply', state='adapter_completed'):
        key = 'key-' + uuid.uuid4().hex
        self.store.claim(message_id, text, key)
        self.store.finish(key, state)
        return key

    def context(self, message_id, **kwargs):
        result = build_conversation_context(self.store, message_id, **kwargs)
        rendered = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        self.assertLessEqual(len(rendered), MAX_CONTEXT_CHARACTERS)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), MAX_CONTEXT_CHARACTERS)
        self.assertNotIn('SECRET_SOURCE_UMO', rendered)
        self.assertLessEqual(len(result['messages']), 8)
        for message in result['messages']:
            self.assertLessEqual(len(message['replies']), 8)
        return result

    def test_private_context_excludes_group_current_and_future(self):
        first = self.ingest('private first')
        self.ingest('group secret', group='300')
        second = self.ingest('private second')
        current = self.ingest('CURRENT_DO_NOT_REPEAT')
        self.ingest('FUTURE_DO_NOT_INCLUDE')
        # Acceptance rowid, rather than wall-clock order, controls selection.
        with self.store.db:
            self.store.db.execute('UPDATE messages SET created=999 WHERE id=?', (first,))
            self.store.db.execute('UPDATE messages SET created=1 WHERE id=?', (second,))
        value = self.context(current)
        self.assertEqual([m['message_id'] for m in value['messages']], [first, second])
        self.assertEqual(value['privacy_scope'], 'private_owner_only')
        self.assertTrue(value['untrusted_content'])
        self.assertIn('not current authorization', value['authorization_note'])
        self.assertFalse(value['history_complete'])
        self.assertFalse(value['limited'])
        self.assertFalse(value['truncated'])
        self.assertNotIn('DO_NOT_', json.dumps(value))
        self.assertNotIn('group secret', json.dumps(value))

    def test_private_history_excludes_a_foreign_private_sender(self):
        other = self.ingest('FOREIGN_PRIVATE_SECRET')
        with self.store.db:
            self.store.db.execute('UPDATE messages SET sender_id=? WHERE id=?', ('999', other))
        own = self.ingest('owner history')
        current = self.ingest()
        self.assertEqual([m['message_id'] for m in self.context(current)['messages']], [own])

    def test_group_context_is_exact_group_and_all_accepted_senders(self):
        self.ingest('PRIVATE_SECRET')
        self.ingest('OTHER_GROUP_SECRET', group='301')
        first = self.ingest('owner in group', group='300')
        second = self.ingest('peer in group', group='300', sender='201')
        current = self.ingest('current', group='300', sender='202')
        self.ingest('FUTURE_SECRET', group='300')
        result = self.context(current)
        self.assertEqual(result['privacy_scope'], 'group_shared')
        self.assertEqual(result['group_id'], '300')
        self.assertEqual([m['message_id'] for m in result['messages']], [first, second])
        self.assertEqual([m['sender_id'] for m in result['messages']], ['200', '201'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_owner_at_narrows_past_senders_and_current_scope(self):
        first = self.ingest('owner', group='300')
        peer = self.ingest('PEER_SECRET', group='300', sender='201')
        current = self.ingest('current', group='300')
        result = self.context(current, group_sender_id='200')
        self.assertEqual([m['message_id'] for m in result['messages']], [first])
        self.assertNotIn('PEER_SECRET', json.dumps(result))
        for mid, sender in ((peer, '200'), (current, '201')):
            with self.subTest(mid=mid, sender=sender), self.assertRaisesRegex(
                    ConversationContextError, '^invalid_history_scope$'):
                self.context(mid, group_sender_id=sender)

    def test_current_row_scope_is_validated(self):
        current = self.ingest()
        for kind, group, sender in (
                ('private', '', '201'), ('private', '300', '200'),
                ('group', 'not-a-group', '200'), ('group', '300', '100'),
                ('group', '300', 'not-a-sender')):
            with self.store.db:
                self.store.db.execute('''UPDATE messages SET conversation_type=?,group_id=?,sender_id=?
                    WHERE id=?''', (kind, group, sender, current))
            with self.subTest(kind=kind, group=group, sender=sender), self.assertRaisesRegex(
                    ConversationContextError, '^invalid_history_scope$'):
                self.context(current)

    def test_current_message_id_validation_and_missing_row(self):
        for mid in ('recipient', '', None, True, 'msg_' + '1' * 33):
            with self.subTest(mid=mid), self.assertRaisesRegex(
                    ConversationContextError, '^invalid_message_id$'):
                self.context(mid)
        with self.assertRaisesRegex(ConversationContextError, '^message_not_found$'):
            self.context('msg_' + '1' * 32)

    def test_only_confirmed_adapter_replies_and_no_invented_flat_order(self):
        first = self.ingest('question one')
        second = self.ingest('question two')
        self.reply(second, 'second answer')
        self.reply(first, 'answer recorded later')
        self.reply(first, 'second successful answer')
        for state in ('uncertain', 'sending', 'not_sent'):
            self.reply(first, 'EXCLUDED_' + state, state)
        current = self.ingest()
        value = self.context(current)
        self.assertEqual([m['message_id'] for m in value['messages']], [first, second])
        self.assertEqual([r['text'] for r in value['messages'][0]['replies']],
                         ['answer recorded later', 'second successful answer'])
        self.assertEqual(value['messages'][1]['replies'][0]['text'], 'second answer')
        self.assertNotIn('EXCLUDED_', json.dumps(value))
        self.assertIn('grouped', value['ordering'])
        for item in value['messages']:
            for reply in item['replies']:
                self.assertEqual(reply['state'], 'adapter_completed')
                self.assertNotIn('timestamp', reply)
                self.assertNotIn('reply_key', reply)

    def test_limits_are_exact_and_newest_retained_rows_are_selected(self):
        ids = [self.ingest(str(n)) for n in range(12)]
        current = self.ingest()
        value = self.context(current)
        self.assertEqual([m['message_id'] for m in value['messages']], ids[-8:])
        self.assertTrue(value['limited'])
        self.assertFalse(value['truncated'])
        for limit in range(1, 9):
            value = self.context(current, limit=limit)
            self.assertEqual([m['message_id'] for m in value['messages']], ids[-limit:])

    def test_disabled_context_does_not_read_database(self):
        db = Mock()
        store = Mock(binding=BINDING, db=db)
        value = build_conversation_context(store, 'parent-already-authorized', limit=0)
        self.assertFalse(value['enabled'])
        self.assertEqual(value['messages'], [])
        db.execute.assert_not_called()

    def test_configuration_is_strict_with_fixed_safe_error(self):
        for value in (None, True, False, -1, 9, 1.0, '8', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ConversationContextError, '^invalid_chat_context_messages$'):
                validate_context_count(value)
        for value in range(9):
            self.assertEqual(validate_context_count(value), value)
        db = Mock()
        with self.assertRaisesRegex(ConversationContextError, '^invalid_chat_context_messages$'):
            build_conversation_context(Mock(db=db), 'unused', limit='SECRET_VALUE')
        db.execute.assert_not_called()

    def test_binding_is_checked_before_sql_for_all_four_components(self):
        current = self.ingest()
        for attr in ('platform_id', 'adapter', 'self_id', 'sender_id'):
            values = dict(platform_id='instance-1', adapter='aiocqhttp', self_id='100', sender_id='200')
            values[attr] = 'other'
            changed = Binding(**values)
            with self.subTest(attr=attr), self.assertRaisesRegex(
                    ConversationContextError, '^binding_changed$'):
                self.context(current, expected_binding=changed)
            # The database cannot be reopened under a different binding either.
            with self.assertRaisesRegex(Rejected, '^binding_changed_requires_new_database$'):
                Store(self.path, changed)
        self.context(current, expected_binding=BINDING)

    def test_unicode_budget_includes_every_body_and_bounded_metadata(self):
        first = self.ingest('你' * 4000)
        for n in range(8):
            self.reply(first, ('🙂' if n % 2 else '界') * 3500)
        current = self.ingest()
        value = self.context(current)
        texts = [m['text'] for m in value['messages']]
        texts.extend(r['text'] for m in value['messages'] for r in m['replies'])
        self.assertLessEqual(sum(map(len, texts)), MAX_CONTEXT_CHARACTERS)
        self.assertEqual(value['messages'][0]['text'], '你' * 4000)
        self.assertTrue(value['limited'])
        self.assertTrue(value['truncated'])
        self.assertTrue(any(r['text_truncated'] for r in value['messages'][0]['replies']))
        self.assertTrue(value['messages'][0]['replies_truncated'])
        # Unicode is counted as characters, not the larger UTF-8 byte size.
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.assertGreater(len(encoded), MAX_CONTEXT_CHARACTERS)

    def test_json_escaping_and_huge_legacy_body_are_bounded_at_sql(self):
        first = self.ingest('original')
        with self.store.db:
            self.store.db.execute('UPDATE messages SET text=? WHERE id=?',
                                  ('\\"\n' * 100000, first))
        current = self.ingest()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            value = self.context(current)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertTrue(value['messages'][0]['text_truncated'])
        self.assertTrue(value['truncated'])
        self.assertTrue(any('substr(text,1,6001)' in sql for sql in statements))
        self.assertFalse(any('SELECT *' in sql for sql in statements))

    def test_newest_message_has_priority_when_bodies_exhaust_budget(self):
        self.ingest('OLDER_SECRET' * 1000)
        latest = self.ingest('newest ' * 1500)
        current = self.ingest()
        value = self.context(current)
        self.assertEqual([m['message_id'] for m in value['messages']], [latest])
        self.assertNotIn('OLDER_SECRET', json.dumps(value))
        self.assertTrue(value['limited'])
        self.assertTrue(value['truncated'])

    def test_sql_nul_preview_is_marked_truncated_without_silent_completeness(self):
        self.ingest('visible\x00remaining')
        current = self.ingest()
        value = self.context(current)
        self.assertEqual(value['messages'][0]['text'], 'visible')
        self.assertTrue(value['messages'][0]['text_truncated'])
        self.assertTrue(value['truncated'])

    def test_media_only_exposes_count_and_does_not_resolve_attachments(self):
        attachments = [{'url': 'https://SECRET_URL.example/a', 'file_id': 'SECRET_FILE_ID',
                        'filename': 'SECRET_FILENAME', 'kind': 'image', 'segment_index': 0}]
        first = self.ingest('attachment question', attachments=attachments)
        current = self.ingest()
        self.store.attachments = Mock()
        value = self.context(current)
        self.store.attachments.assert_not_called()
        self.assertFalse(self.store.attachments.mock_calls)
        self.assertEqual(value['messages'][0]['message_id'], first)
        self.assertEqual(value['messages'][0]['attachment_count'], 1)
        self.assertNotIn('SECRET_', json.dumps(value))

    def test_corrupt_and_oversize_media_have_unknown_count_without_content(self):
        first = self.ingest()
        current = self.ingest()
        for raw in ('{bad', '"SECRET"', '["' + 'SECRET' * 12000 + '"]', '[' * 3000):
            with self.store.db:
                self.store.db.execute('UPDATE messages SET media_json=? WHERE id=?', (raw, first))
            value = self.context(current)
            self.assertIsNone(value['messages'][0]['attachment_count'])
            self.assertNotIn('SECRET', json.dumps(value))

    def test_historical_metadata_is_bounded_or_rejected(self):
        first = self.ingest(group='300')
        current = self.ingest(group='300')
        with self.store.db:
            self.store.db.execute('UPDATE messages SET sender_id=? WHERE id=?', ('x' * 20000, first))
        with self.assertRaisesRegex(ConversationContextError, '^invalid_history_scope$'):
            self.context(current)

    def test_corrupt_extra_replies_are_still_bounded_and_deterministic(self):
        first = self.ingest()
        with self.store.db:
            for n in range(12):
                self.store.db.execute('INSERT INTO replies VALUES (?,?,?,?)',
                                      ('extra-' + str(n), first, str(n), 'adapter_completed'))
        current = self.ingest()
        value = self.context(current)
        self.assertEqual([r['text'] for r in value['messages'][0]['replies']], list(map(str, range(8))))
        self.assertTrue(value['messages'][0]['replies_truncated'])
        self.assertTrue(value['limited'])

    def test_reads_never_modify_processing_or_delivery_state(self):
        first = self.ingest()
        self.reply(first, state='uncertain')
        current = self.ingest()
        before = list(self.store.db.iterdump())
        changes = self.store.db.total_changes
        self.context(current)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(self.store.read(first)['processing_state'], 'pending')
        self.assertEqual(self.store.read(current)['processing_state'], 'pending')

    def test_restart_preserves_context_and_does_not_include_uncertain_sends(self):
        first = self.ingest('history survives')
        self.reply(first, 'completed survives')
        second = self.ingest('unfinished')
        self.store.claim(second, 'SENDING_SECRET', 'interrupted')
        current = self.ingest()
        before = self.context(current)
        self.store.close()
        self.store = Store(self.path, BINDING)
        after = self.context(current)
        self.assertEqual(before, after)
        self.assertNotIn('SENDING_SECRET', json.dumps(after))

    def test_pruned_history_does_not_fabricate_completeness_or_pruning_detection(self):
        first = self.ingest('PRUNED_SECRET')
        self.store.history.complete(first)
        current = self.ingest()
        self.assertEqual(self.store.history.prune(max_messages=1), 1)
        value = self.context(current)
        self.assertEqual(value['messages'], [])
        self.assertFalse(value['history_complete'])
        self.assertFalse(value['limited'])
        self.assertFalse(value['truncated'])
        self.assertIn('may omit', value['completeness_note'])
        self.assertNotIn('PRUNED_SECRET', json.dumps(value))
        self.store.close()
        self.store = Store(self.path, BINDING)
        self.assertEqual(value, self.context(current))


if __name__ == '__main__':
    unittest.main()
