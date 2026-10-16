"""v0.5 conversation permissions, migrations and current-policy revocation.

Synthetic raw/normalized events only. No live bot or network interaction.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, AsyncMock

from test_host import Binding, Store, Rejected, snapshot
from test_main import Event, Plain, At, load_main
from test_group_modes import all_event
from test_groups import group_event
from access_policy import AccessPolicy, PolicyError

BINDING = Binding('instance-1', 'aiocqhttp', '100', '200')


def rule(group='300', mode='at', scope='all', senders=()):
    return {'group_id': group, 'mode': mode, 'at_sender_scope': scope,
            'sender_ids': list(senders)}


def private_event(sender='201', source='original-1'):
    event = Event()
    event.sender = sender
    event.message_obj.raw_message['user_id'] = int(sender)
    event.message_obj.raw_message['sender']['user_id'] = int(sender)
    event.message_obj.raw_message['message_id'] = source
    event.message_obj.message_id = source
    event.unified_msg_origin = 'instance-1:FriendMessage:' + sender
    return event


def mentioned_event(sender='201', group='300'):
    event = group_event(group)
    event.sender = sender
    event.message_obj.raw_message['user_id'] = int(sender)
    event.message_obj.raw_message['sender']['user_id'] = int(sender)
    return event


class PolicyParserTests(unittest.TestCase):
    def test_missing_at_scope_defaults_selected_and_never_allows_unknown_member(self):
        policy = self.policy(access_rules_version=1, group_rules=[{'group_id':'300','mode':'at'}])
        self.assertEqual(policy.group_rule('300').at_sender_scope, 'selected')
        self.assertFalse(policy.authorizes_stored({'conversation_type':'group','group_id':'300','sender_id':'999'}))
        policy = self.policy(access_rules_version=1, group_rules=[{'group_id':'300','mode':'at','sender_ids':['201']}])
        self.assertTrue(policy.authorizes_stored({'conversation_type':'group','group_id':'300','sender_id':'201'}))
        self.assertFalse(policy.authorizes_stored({'conversation_type':'group','group_id':'300','sender_id':'999'}))

    def policy(self, **config):
        return AccessPolicy.from_config(config, BINDING)

    def test_legacy_missing_and_empty_lists_keep_owner_at(self):
        for extras in ({}, {'private_sender_ids': [], 'group_rules': []}):
            policy = self.policy(group_ids=['300'], **extras)
            self.assertEqual(policy.private_sender_ids, frozenset({'200'}))
            group = policy.group_rule('300')
            self.assertEqual((group.mode, group.at_sender_scope, group.sender_ids),
                             ('at', 'selected', frozenset({'200'})))

    def test_legacy_all_messages_stays_all_in_allowed_groups_only(self):
        policy = self.policy(group_ids=['300'], group_trigger_mode='all_messages')
        self.assertEqual(policy.group_rule('300').mode, 'all')
        self.assertIsNone(policy.group_rule('400'))

    def test_version_one_empty_lists_disable_both_scopes(self):
        policy = self.policy(access_rules_version=1, group_ids=['300'],
                             private_sender_ids=[], group_rules=[])
        self.assertEqual(policy.private_sender_ids, frozenset())
        self.assertEqual(policy.group_rules, ())
        self.assertFalse(policy.authorizes_stored(
            {'conversation_type': 'private', 'group_id': '', 'sender_id': '200'}))

    def test_explicit_rules_authoritative_even_with_invalid_legacy_config(self):
        policy = self.policy(group_ids=None, group_trigger_mode='invalid',
                             group_rules=[rule('400', 'selected', senders=['201'])])
        self.assertIsNone(policy.group_rule('300'))
        self.assertTrue(policy.group_rule('400').allows_sender('201'))
        self.assertFalse(policy.group_rule('400').allows_sender('200'))

    def test_new_private_list_replaces_owner_without_changing_binding(self):
        policy = self.policy(private_sender_ids=['201', '202'])
        self.assertEqual(policy.private_sender_ids, frozenset({'201', '202'}))
        self.assertEqual(policy.binding, BINDING)
        self.assertFalse(policy.authorizes_stored(
            {'conversation_type': 'private', 'group_id': '', 'sender_id': '200'}))

    def test_malformed_private_lists_fail_whole_policy(self):
        for ids in (None, '201', [201], [True], ['0'], ['01'], ['-1'], ['2\n'],
                    ['201', '201'], ['100'], [str(i) for i in range(200, 301)]):
            with self.subTest(ids=ids), self.assertRaises(PolicyError):
                self.policy(private_sender_ids=ids, group_rules=[rule(mode='all')])

    def test_malformed_rules_fail_whole_policy(self):
        bad_rules = [None, {}, [None], [rule(), rule()],
                     [rule(group='01')], [rule(mode='owner_at')],
                     [rule(scope='owner')], [rule(senders=['100'])],
                     [rule(senders=['201', '201'])],
                     [{**rule(), 'sender_ids': [201]}],
                     [{**rule(), 'unknown': True}],
                     [{**rule(), '__template_key': 'other'}],
                     [{'mode': 'at'}], [{'group_id': '300'}],
                     [rule(group=str(i)) for i in range(300, 401)]]
        for rules in bad_rules:
            with self.subTest(rules=rules), self.assertRaises(PolicyError):
                self.policy(group_rules=rules)

    def test_only_supported_integer_policy_versions(self):
        for version in (True, False, None, '1', 2, -1):
            with self.subTest(version=version), self.assertRaises(PolicyError):
                self.policy(access_rules_version=version)

    def test_template_marker_optional_but_exact(self):
        policy = self.policy(group_rules=[{**rule(), '__template_key': 'group'}])
        self.assertEqual(policy.group_rule('300').mode, 'at')

    def test_selected_empty_sender_list_denies_everyone(self):
        for group in (rule(mode='selected'), rule(scope='selected')):
            self.assertFalse(self.policy(group_rules=[group]).group_rule('300').allows_sender('201'))

    def test_policy_is_independent_of_mutable_config_lists(self):
        config = {'private_sender_ids': ['201'], 'group_rules': [rule(senders=['202'])]}
        policy = AccessPolicy.from_config(config, BINDING)
        config['private_sender_ids'].append('203')
        config['group_rules'][0]['sender_ids'].append('204')
        self.assertNotIn('203', policy.private_sender_ids)
        self.assertNotIn('204', policy.group_rule('300').sender_ids)

    def test_stored_check_validates_actual_sender_and_route(self):
        policy = self.policy(private_sender_ids=['201'], group_rules=[rule(mode='all')])
        for row in ({}, {'conversation_type': 'group', 'group_id': '300', 'sender_id': '100'},
                    {'conversation_type': 'group', 'group_id': '400', 'sender_id': '201'},
                    {'conversation_type': 'private', 'group_id': '300', 'sender_id': '201'},
                    {'conversation_type': 'private', 'group_id': '', 'sender_id': True},
                    {'conversation_type': 'other', 'group_id': '', 'sender_id': '201'}):
            self.assertFalse(policy.authorizes_stored(row))

    def test_optional_snapshot_policy_and_wrong_binding(self):
        policy = self.policy(private_sender_ids=['201'])
        self.assertEqual(snapshot(private_event(), BINDING, Plain, At,
                                  policy=policy)['sender_id'], '201')
        other = Binding('other', 'aiocqhttp', '100', '200')
        with self.assertRaisesRegex(Rejected, 'invalid_access_policy'):
            snapshot(private_event(), BINDING, Plain, At,
                     policy=AccessPolicy.from_config({}, other))
        with self.assertRaises(Rejected):
            snapshot(private_event(), BINDING, Plain, At)

    def test_standalone_store_never_accepts_arbitrary_private_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'inbox.db', BINDING)
            try:
                with self.assertRaisesRegex(Rejected, 'invalid_conversation'):
                    store.ingest(snapshot(private_event(), BINDING, Plain, At,
                                          policy=self.policy(private_sender_ids=['201'])))
            finally:
                store.close()


class PolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_main(self.temp.name)
        self.config = {'enabled': True, 'platform_id': 'instance-1', 'adapter': 'aiocqhttp',
                       'self_id': '100', 'sender_id': '200', 'mcp_token': 'x' * 32,
                       'private_sender_ids': ['200', '201', '202'],
                       'group_rules': [rule('300'), rule('400', 'selected', senders=['201']),
                                       rule('500', 'all')]}
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def accept(self, event):
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        return next(reversed(self.plugin.captured))

    async def reject(self, event):
        count = self.plugin.host.store.status()['messages']
        await self.plugin._on_message(event)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin.host.store.status()['messages'], count)

    async def test_multiple_private_users_keep_identities_and_same_route_replies(self):
        first, second, owner = private_event('201'), private_event('202'), private_event('200')
        mids = [await self.accept(event) for event in (first, second, owner)]
        self.assertEqual(len(set(mids)), 3)
        store = self.plugin.host.store
        for mid, sender, scope in zip(mids, ('201', '202', '200'),
                                     ('private_sender_only', 'private_sender_only', 'private_owner_only')):
            data = store.read(mid)
            self.assertEqual((data['sender_id'], data['privacy_scope']), (sender, scope))
            self.assertIsNone(self.plugin._validate_read_message(mid))
            self.assertNotIn('conversation_context', data)
        result = await self.plugin.host.reply(mids[1], 'only second user', 'second')
        self.assertEqual(result['state'], 'adapter_completed')
        self.assertEqual(first.sent, [])
        self.assertEqual(owner.sent, [])
        self.assertEqual(second.sent[0][0].text, 'only second user')
        self.assertEqual(store.binding.sender_id, '200')

    async def test_unauthorized_private_and_bot_echo_not_stopped(self):
        await self.reject(private_event('299'))
        await self.reject(private_event('100'))
        await self.reject(all_event('100', '500'))

    async def test_group_at_all_senders_selected_senders_and_all_modes(self):
        await self.accept(mentioned_event('202', '300'))
        await self.reject(all_event('202', '300'))
        await self.accept(all_event('201', '400'))
        await self.reject(mentioned_event('202', '400'))
        await self.accept(all_event('202', '500'))
        await self.reject(mentioned_event('201', '999'))

    async def test_at_selected_scope_requires_sender_and_real_top_level_mention(self):
        self.config['group_rules'] = [rule(scope='selected', senders=['201'])]
        await self.accept(mentioned_event('201'))
        await self.reject(mentioned_event('202'))
        await self.reject(all_event('201'))
        for segment in ({'type': 'text', 'data': {'text': '@100 [CQ:at,qq=100]'}},
                        {'type': 'at', 'data': {'qq': 'all'}},
                        {'type': 'at', 'data': {'qq': '999'}},
                        {'type': 'reply', 'data': {'id': 'x', 'chain': [
                            {'type': 'at', 'data': {'qq': '100'}}]}},
                        {'type': 'node', 'data': {'content': [
                            {'type': 'at', 'data': {'qq': '100'}}]}}):
            event = all_event('201')
            event.message_obj.raw_message['message'].append(segment)
            if segment['type'] == 'text':
                event.messages = [Plain('hello from alt' + segment['data']['text'])]
            await self.reject(event)

    async def test_raw_normalized_sender_route_and_source_still_required(self):
        for key, value in [('user_id', 202), ('sender', {'user_id': 202}), ('self_id', 999),
                           ('message_id', 'other'), ('group_id', 300)]:
            event = private_event('201')
            event.message_obj.raw_message[key] = value
            await self.reject(event)
        event = mentioned_event('201')
        event.messages = [Plain('hello'), At('999'), Plain('world')]
        await self.reject(event)

    async def test_invalid_any_rule_blocks_even_otherwise_allowed_private(self):
        self.config['group_rules'].append(rule('300', 'all'))
        await self.reject(private_event('201'))
        await self.reject(all_event('202', '500'))

    async def test_private_removal_revokes_read_send_and_direct_ingest(self):
        event = private_event('201')
        mid = await self.accept(event)
        value = self.plugin._snapshot(private_event('201', 'next'))
        self.config['private_sender_ids'] = ['200', '202']
        for read in (self.plugin._validate_read_message, self.plugin.host.store.read):
            with self.assertRaisesRegex(Exception, '^binding_inactive$'):
                read(mid)
        with self.assertRaisesRegex(Exception, '^invalid_conversation$'):
            self.plugin.host.store.ingest(value)
        self.assertEqual((await self.plugin.host.reply(mid, 'revoked', 'revoked'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])
        await self.reject(private_event('201', 'later'))

    async def test_narrowed_group_sender_revokes_read_and_send(self):
        event = all_event('202', '500')
        mid = await self.accept(event)
        self.config['group_rules'][-1] = rule('500', 'selected', senders=['201'])
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            self.plugin._validate_read_message(mid)
        self.assertEqual((await self.plugin.host.reply(mid, 'revoked', 'revoked'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_all_to_at_requires_live_mention_for_send_but_stored_membership_for_read(self):
        event = all_event('202', '500')
        mid = await self.accept(event)
        self.config['group_rules'][-1] = rule('500', 'at')
        self.assertIsNone(self.plugin._validate_read_message(mid))
        self.assertEqual((await self.plugin.host.reply(mid, 'needs at', 'new-at'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_private_revocation_after_preflight_blocks_adapter(self):
        event = private_event('201')
        mid = await self.accept(event)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.plugin.host.preflight
        async def waiting(row):
            target = await original(row)
            entered.set()
            await release.wait()
            return target
        self.plugin.host.preflight = waiting
        task = asyncio.create_task(self.plugin.host.reply(mid, 'late', 'late'))
        await entered.wait()
        self.config['private_sender_ids'] = ['200']
        release.set()
        self.assertEqual((await task)['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_mutating_secondary_private_identity_cannot_retarget_reply(self):
        event = private_event('201')
        mid = await self.accept(event)
        event.sender = '202'
        event.message_obj.raw_message['user_id'] = 202
        event.message_obj.raw_message['sender']['user_id'] = 202
        self.assertEqual((await self.plugin.host.reply(mid, 'retarget', 'retarget'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_private_list_change_survives_restart_without_rebinding_sqlite(self):
        event = private_event('201')
        mid = await self.accept(event)
        path, binding = self.plugin.host.store.path, self.plugin.host.store.binding
        self.config['private_sender_ids'] = ['201', '202']
        await self.plugin.terminate()
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertEqual((self.plugin.host.store.path, self.plugin.host.store.binding), (path, binding))
        self.assertEqual(self.plugin.host.store.read(mid)['sender_id'], '201')
        self.assertIsNone(self.plugin._validate_read_message(mid))
        self.assertEqual((await self.plugin.host.reply(mid, 'old handle', 'old'))['state'], 'not_sent')
        await self.reject(private_event('200'))

    async def test_disabled_or_stopping_revokes_store_reads_pending_and_history(self):
        mid = await self.accept(private_event('201'))
        store = self.plugin.host.store
        reads = (lambda: store.read(mid),
                 lambda: store.history.pending('private', sender_id='201'),
                 lambda: store.history.page('private', sender_id='201'),
                 lambda: store.history.conversations())
        self.config['enabled'] = False
        for read in reads:
            with self.assertRaisesRegex(Exception, '^binding_inactive$'):
                read()
        self.config['enabled'] = True
        self.plugin.host.stopping = True
        for read in reads:
            with self.assertRaisesRegex(Exception, '^binding_inactive$'):
                read()
        self.plugin.host.stopping = False
        self.assertEqual(store.history.pending('private', sender_id='201')['messages'][0]['message_id'], mid)

    async def test_version_one_empty_lists_override_legacy_allowlist(self):
        self.config.update(access_rules_version=1, private_sender_ids=[], group_rules=[], group_ids=['300'])
        await self.reject(private_event('200'))
        await self.reject(mentioned_event('200'))


class PolicyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_main(self.temp.name)
        class Config(dict):
            saves = 0
            fail = False
            def save_config(inner):
                inner.saves += 1
                if inner.fail:
                    raise OSError('private failure details')
                inner.saved = deepcopy(dict(inner))
        self.config = Config(enabled=False, platform_id='instance-1', adapter='aiocqhttp',
                             self_id='100', sender_id='200', mcp_token='x' * 32,
                             group_ids=['300', '400'])
        self.plugin = self.module.Main(object(), self.config)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_disabled_upgrade_saves_exact_legacy_scope_once_without_startup(self):
        await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)
        self.assertIsNone(self.plugin.host)
        self.assertEqual(self.config.saves, 1)
        self.assertEqual(self.config.saved, dict(self.config))
        self.assertEqual(self.config['access_rules_version'], 1)
        self.assertEqual(self.config['private_sender_ids'], ['200'])
        self.assertEqual(self.config['sender_id'], '200')
        for entry in self.config['group_rules']:
            self.assertEqual((entry['mode'], entry['at_sender_scope'], entry['sender_ids']),
                             ('at', 'selected', ['200']))
        await self.plugin.initialize()
        self.assertEqual(self.config.saves, 1)

    async def test_all_messages_upgrade_and_existing_explicit_private_list(self):
        self.config.update(group_trigger_mode='all_messages', private_sender_ids=['202', '201'])
        await self.plugin.initialize()
        self.assertEqual(self.config['private_sender_ids'], ['202', '201'])
        self.assertEqual([x['mode'] for x in self.config['group_rules']], ['at', 'at'])
        self.assertTrue(all(x['at_sender_scope'] == 'all' for x in self.config['group_rules']))
        self.assertTrue(all(x['sender_ids'] == [] for x in self.config['group_rules']))

    async def test_explicit_selected_becomes_at_selected_and_legacy_private_filled(self):
        explicit = [rule('999', 'selected', senders=['202'])]
        self.config.update(group_rules=deepcopy(explicit), group_ids=None, group_trigger_mode='bad')
        await self.plugin.initialize()
        self.assertEqual(self.config['group_rules'], [{**explicit[0],
                         'mode': 'at', '__template_key': 'group', 'at_sender_scope': 'selected'}])
        self.assertEqual(self.config['private_sender_ids'], ['200'])
        self.assertEqual(self.config['access_rules_version'], 1)

    async def test_explicit_modes_migrate_once_to_at_with_original_sender_scope(self):
        self.config['group_rules'] = [rule('300', 'all'),
            rule('400', 'selected', senders=['201', '202']),
            rule('500', 'at', 'selected', ['202']), rule('600', 'at', 'all')]
        await self.plugin.initialize()
        groups = self.config['group_rules']
        self.assertEqual([entry['mode'] for entry in groups], ['at'] * 4)
        self.assertEqual([entry['at_sender_scope'] for entry in groups],
                         ['all', 'selected', 'selected', 'all'])
        self.assertEqual([entry['sender_ids'] for entry in groups],
                         [[], ['201', '202'], ['202'], []])
        self.config['group_rules'][0]['mode'] = 'all'
        self.config['group_rules'][1]['mode'] = 'selected'
        await self.plugin.initialize()
        self.assertEqual(self.config['group_rules'][0]['mode'], 'all')
        self.assertEqual(self.config['group_rules'][1]['mode'], 'selected')
        self.assertEqual(self.config.saves, 1)

    async def test_upgraded_busy_group_ignores_unmentioned_traffic_before_ingest(self):
        self.config.update(enabled=True, group_trigger_mode='all_messages')
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        store = self.plugin.host.store
        store.events = types.SimpleNamespace(enqueue=Mock(return_value=0), notify_enqueued=Mock())
        original_register = store.attachments.register
        store.attachments.register = Mock(wraps=original_register)
        store.attachments.transport.download = AsyncMock()
        for index in range(500):
            event = all_event('201', '300', 'busy group message ' + str(index))
            event.message_obj.message_id = str(index)
            event.message_obj.raw_message['message_id'] = str(index)
            await self.plugin.on_group_message(event)
            self.assertFalse(event.stopped)
        self.assertEqual(store.status()['messages'], 0)
        self.assertEqual(self.plugin.captured, {})
        store.events.enqueue.assert_not_called()
        store.events.notify_enqueued.assert_not_called()
        store.attachments.register.assert_not_called()
        store.attachments.transport.download.assert_not_called()
        event = mentioned_event('201', '300')
        await self.plugin.on_group_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(store.status()['messages'], 1)
        store.events.enqueue.assert_called_once()
        store.attachments.transport.download.assert_not_called()
        store.events = None

    async def test_save_failure_restores_all_fields_and_opens_no_database(self):
        self.config['enabled'] = True
        before = deepcopy(dict(self.config))
        self.config.fail = True
        await self.plugin.initialize()
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.config.saves, 1)
        self.assertEqual(self.plugin.runtime_error, 'access_policy_migration_failed')
        self.assertFalse(self.plugin.ready)
        self.assertIsNone(self.plugin.host)
        self.assertFalse(list(Path(self.temp.name).rglob('inbox.sqlite3')))

    async def test_malformed_legacy_policy_never_migrates_or_starts(self):
        self.config.update(enabled=True, group_ids=['300', '300'])
        before = deepcopy(dict(self.config))
        await self.plugin.initialize()
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.config.saves, 0)
        self.assertFalse(self.plugin.ready)
        self.assertIsNone(self.plugin.host)

    async def test_authoritative_empty_lists_stay_empty_on_restart(self):
        self.config.update(enabled=True, access_rules_version=1, private_sender_ids=[], group_rules=[])
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertEqual(self.config.saves, 0)
        event = private_event('200')
        await self.plugin._on_message(event)
        self.assertFalse(event.stopped)
        await self.plugin.terminate()
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertEqual(self.config['private_sender_ids'], [])
        self.assertEqual(self.config['group_rules'], [])


if __name__ == '__main__':
    unittest.main()
