"""Synthetic current-policy isolation for v0.5 history and pending queries."""
from types import SimpleNamespace
import unittest

import test_history_store as fixture
from access_policy import AccessPolicy
from host import Binding, Store


class ScopedHistoryTests(unittest.TestCase):
    setUp = fixture.HistoryTests.setUp
    tearDown = fixture.HistoryTests.tearDown
    value = fixture.HistoryTests.value
    insert = fixture.HistoryTests.insert

    def policy(self, private=('200', '201'), groups=None):
        self.binding = Binding('synthetic', 'mock', '100', '200')
        config = {'private_sender_ids': list(private), 'group_rules': groups or []}
        self.active_policy = AccessPolicy.from_config(config, self.binding)
        self.history.policy_provider = lambda: self.active_policy
        self.history.private_sender_id = self.binding.sender_id
        self.history.privacy_scope = lambda row: ('group_shared' if row['conversation_type'] == 'group'
            else 'private_owner_only' if row['sender_id'] == '200' else 'private_sender_only')

    def test_multiple_private_senders_require_selection_even_if_only_one_has_rows(self):
        self.policy()
        self.insert()
        for query in (self.history.page, self.history.pending):
            with self.assertRaisesRegex(fixture.HistoryError, '^private_sender_required$'):
                query('private')
            for sender in ('202', '', '1 OR 1=1', 200, True):
                with self.assertRaisesRegex(fixture.HistoryError, '^invalid_history_scope$'):
                    query('private', sender_id=sender)

    def test_private_pages_and_cursors_never_mix_senders_or_query_actions(self):
        self.policy()
        expected = {sender: [self.insert(self.value(sender_id=sender), created=10)
                            for _ in range(3)] for sender in ('200', '201')}
        for query in (self.history.page, self.history.pending):
            for sender in expected:
                first = query('private', sender_id=sender, limit=2)
                second = query('private', sender_id=sender, limit=2, cursor=first['next_cursor'])
                self.assertEqual([row['message_id'] for page in (first, second)
                                  for row in page['messages']], list(reversed(expected[sender])))
                self.assertIsNone(second['next_cursor'])
                self.assertTrue(all(row['sender_id'] == sender for row in first['messages']))
                self.assertEqual(first['messages'][0]['privacy_scope'],
                                 'private_owner_only' if sender == '200' else 'private_sender_only')
                with self.assertRaisesRegex(fixture.HistoryError, '^invalid_history_cursor$'):
                    query('private', sender_id='201' if sender == '200' else '200',
                          cursor=first['next_cursor'])
        page = self.history.page('private', sender_id='200', limit=1)
        with self.assertRaisesRegex(fixture.HistoryError, '^invalid_history_cursor$'):
            self.history.pending('private', sender_id='200', cursor=page['next_cursor'])

    def test_single_allowed_nonowner_preserves_omitted_argument_without_owner_leak(self):
        self.policy(private=('201',))
        self.insert(self.value(sender_id='200', text='revoked-owner'))
        selected = self.insert(self.value(sender_id='201', text='selected'))
        for query in (self.history.page, self.history.pending):
            result = query('private')
            self.assertEqual([row['message_id'] for row in result['messages']], [selected])
        rows = self.history.conversations()['conversations']
        self.assertEqual([(row['sender_id'], row['message_count']) for row in rows], [('201', 1)])

    def test_policy_change_immediately_revokes_rows_and_preserves_durable_data(self):
        self.policy()
        self.insert(self.value(sender_id='200'))
        selected = self.insert(self.value(sender_id='201'))
        self.policy(private=('201',))
        with self.assertRaisesRegex(fixture.HistoryError, '^invalid_history_scope$'):
            self.history.page('private', sender_id='200')
        self.assertEqual(self.history.pending('private')['messages'][0]['message_id'], selected)
        self.assertEqual(len(self.history.conversations()['conversations']), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_group_is_whole_conversation_but_current_sender_rules_filter_before_counts(self):
        rules = [{'group_id': '300', 'mode': 'all'}, {'group_id': '400', 'mode': 'all'}]
        self.policy(groups=rules)
        first = self.insert(self.value(conversation_type='group', group_id='300', sender_id='201'))
        self.insert(self.value(conversation_type='group', group_id='300', sender_id='202'))
        self.insert(self.value(conversation_type='group', group_id='400', sender_id='202'))
        initial = self.history.conversations()['conversations']
        self.assertEqual(initial[0]['message_count'], 2)
        self.assertIsNone(initial[0]['sender_id'])
        self.policy(groups=[{'group_id': '300', 'mode': 'selected', 'sender_ids': ['201']}])
        for query in (self.history.page, self.history.pending):
            rows = query('group', '300')['messages']
            self.assertEqual([row['message_id'] for row in rows], [first])
            self.assertEqual(rows[0]['privacy_scope'], 'group_shared')
            self.assertEqual(query('group', '400')['messages'], [])
            with self.assertRaisesRegex(fixture.HistoryError, '^invalid_history_scope$'):
                query('group', '300', sender_id='201')
        rows = self.history.conversations()['conversations']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['message_count'], rows[0]['pending_count']), (1, 1))

    def test_bounded_conversations_paginate_by_private_sender_not_one_private_bucket(self):
        senders = tuple(str(200 + number) for number in range(60))
        self.policy(private=senders)
        for sender in senders:
            self.insert(self.value(sender_id=sender), admit=False)
        # Revoked records sort before/between permitted records, and must not
        # consume page capacity or disappear behind a misleading end cursor.
        for sender in ('199', '2200', '999'):
            self.insert(self.value(sender_id=sender), admit=False)
        first = self.history.conversations(limit=50)
        second = self.history.conversations(limit=50, cursor=first['next_cursor'])
        self.assertEqual(len(first['conversations']), 50)
        self.assertEqual(len(second['conversations']), 10)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual([row['sender_id'] for page in (first, second)
                          for row in page['conversations']], list(senders))

    def test_pending_filters_completion_and_revoked_rows_before_limit(self):
        self.policy(groups=[{'group_id': '300', 'mode': 'selected', 'sender_ids': ['201']}])
        valid = [self.insert(self.value(conversation_type='group', group_id='300', sender_id='201'),
                             created=index, completed=index == 0) for index in range(5)]
        for index in range(30):
            self.insert(self.value(conversation_type='group', group_id='300', sender_id='202'),
                        created=100 + index, admit=False)
        first = self.history.pending('group', '300', limit=2)
        second = self.history.pending('group', '300', limit=2, cursor=first['next_cursor'])
        self.assertEqual([row['message_id'] for page in (first, second) for row in page['messages']],
                         list(reversed(valid[1:])))
        self.assertIsNone(second['next_cursor'])

    def test_legacy_standalone_never_silently_mixes_private_senders(self):
        self.insert(self.value(sender_id='200'))
        self.insert(self.value(sender_id='201'))
        for query in (self.history.page, self.history.pending):
            with self.assertRaisesRegex(fixture.HistoryError, '^private_sender_required$'):
                query('private')
            self.assertEqual(len(query('private', sender_id='200')['messages']), 1)

    def test_policy_snapshot_taken_once_per_query(self):
        self.policy()
        self.insert()
        calls = []
        def current():
            calls.append(1)
            return self.active_policy
        self.history.policy_provider = current
        self.history.page('private', sender_id='200')
        self.history.pending('private', sender_id='200')
        self.history.conversations()
        self.assertEqual(len(calls), 3)

    def test_policy_failure_cannot_reuse_previous_authorized_snapshot(self):
        self.policy()
        self.insert()
        self.assertEqual(len(self.history.page('private', sender_id='200')['messages']), 1)
        def invalid_policy():
            raise fixture.HistoryError('binding_inactive')
        self.history.policy_provider = invalid_policy
        for query, args in ((self.history.page, ('private',)),
                            (self.history.pending, ('private',)),
                            (self.history.conversations, ())):
            with self.assertRaisesRegex(fixture.HistoryError, '^binding_inactive$'):
                query(*args)

    def test_authenticated_admin_accepts_sender_selector_and_preserves_fixed_errors(self):
        from test_main import load_main
        self.policy()
        selected = self.insert(self.value(sender_id='201'))
        admin_type = load_main(self.temp.name).Admin
        plugin = SimpleNamespace(ready=True, host=SimpleNamespace(store=SimpleNamespace(
            history=self.history, attachments=None)))
        admin = admin_type(plugin)
        request = SimpleNamespace(username='synthetic-admin', plugin_name='astrbot_plugin_dot_private')
        payload = dict(csrf_token=admin.authorize(request), action='messages',
                       conversation_type='private', group_id='', before=None, limit=30)
        with self.assertRaisesRegex(Exception, '^private_sender_required$'):
            admin.history(request, payload)
        result = admin.history(request, {**payload, 'sender_id': '201'})
        self.assertEqual([row['message_id'] for row in result['messages']], [selected])
        self.assertEqual(result['messages'][0]['processing_state'], 'pending')
        self.assertNotIn('next_cursor', result)
        for extra in ({'sender_id': '999'}, {'sender_id': '201', 'sql': 'SELECT * FROM messages'}):
            with self.assertRaisesRegex(Exception, '^(invalid_history_scope|invalid_history_request)$'):
                admin.history(request, {**payload, **extra})
        with self.assertRaisesRegex(Exception, '^forbidden$'):
            admin.history(request, {**payload, 'sender_id': '201', 'csrf_token': 'wrong'})

    def test_store_constructor_wires_policy_and_owner_privacy(self):
        self.policy()
        store = Store(self.path.parent / 'store.sqlite3', self.binding,
                      policy_provider=lambda: self.active_policy)
        try:
            mid, _ = store.ingest(self.value(sender_id='201'))
            with self.assertRaisesRegex(Exception, '^private_sender_required$'):
                store.history.pending('private')
            row = store.history.page('private', sender_id='201')['messages'][0]
            self.assertEqual(row['message_id'], mid)
            self.assertEqual(row['privacy_scope'], 'private_sender_only')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
