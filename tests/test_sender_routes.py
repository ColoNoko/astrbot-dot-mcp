"""Sender-routed runtimes stay isolated behind the existing permission policy.

All events and endpoint lifecycles are synthetic. These tests never open a
listener, start a tunnel, or contact a real QQ account.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_main import Event, At, load_main
from sender_routes import RouteError, parse_routes


def route(sender, port, marker):
    return {'sender_id': sender, 'listen_port': port,
            'tunnel_id': 'tunnel_' + marker * 32}


def group_rule(group='300', mode='all', scope='all', senders=()):
    return {'group_id': group, 'mode': mode, 'at_sender_scope': scope,
            'sender_ids': list(senders)}


def event_for(sender='201', source='shared-source', group=None, mention=False):
    event = Event()
    event.sender = sender
    event.message_obj.message_id = source
    event.message_obj.raw_message.update(
        user_id=int(sender), sender={'user_id': int(sender)}, message_id=source)
    event.unified_msg_origin = 'instance-1:FriendMessage:' + sender
    if group is not None:
        event.private = False
        event.group = group
        event.unified_msg_origin = 'instance-1:GroupMessage:' + sender + '_' + group
        event.message_obj.raw_message.update(message_type='group', group_id=int(group))
    if mention:
        event.message_obj.raw_message['message'].append({'type': 'at', 'data': {'qq': '100'}})
        event.messages.append(At('100'))
    return event


class RecordingEndpoint:
    """Record constructor routing without using a network implementation."""
    instances = []

    def __init__(self, host, token, callback_hosts, listen_host, listen_port, **kwargs):
        self.host = host
        self.token = token
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.started = False
        self.closed = False
        self.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True


class RecordingTunnel:
    """Fake only the supervisor, so Main still executes its real tunnel setup."""
    instances = []

    def __init__(self, private, binary):
        self.private = Path(private)
        self.binary = Path(binary)
        self.running = False
        self.close_behavior = 'normal'
        self.close_count = 0
        self.instances.append(self)

    async def start(self, settings, *, endpoint_url, mcp_token, proxy):
        self.settings = settings
        self.endpoint_url = endpoint_url
        self.mcp_token = mcp_token
        self.runtime_key = Path(settings.runtime_key_file).read_text()
        self.running = True

    def status(self):
        return {'enabled': True, 'state': 'running' if self.running else 'stopped',
                'process_running': None if self.close_behavior == 'uncertain' else self.running,
                'healthy': self.running, 'ready': self.running,
                'last_error_code': None}

    async def close(self):
        self.close_count += 1
        if self.close_behavior == 'raise':
            raise RuntimeError('synthetic private tunnel cleanup detail')
        if self.close_behavior != 'uncertain':
            self.running = False


class SenderRouteParserTests(unittest.TestCase):
    def config(self, routes):
        return {'self_id': '100', 'sender_routes': routes}

    def test_missing_and_empty_routes_keep_legacy_mode(self):
        self.assertEqual(parse_routes({}), ())
        self.assertEqual(parse_routes(self.config([])), ())

    def test_valid_routes_preserve_order_without_mutating_config(self):
        config = self.config([route('201', 18770, 'a'), route('202', 18771, 'b')])
        original = deepcopy(config)
        parsed = parse_routes(config)
        self.assertEqual([(item.sender_id, item.tunnel_id, item.listen_port) for item in parsed],
                         [('201', 'tunnel_' + 'a' * 32, 18770),
                          ('202', 'tunnel_' + 'b' * 32, 18771)])
        self.assertEqual(config, original)

    def test_optional_template_marker_and_qualified_tunnel_id(self):
        item = dict(route('201', 1024, 'a'), __template_key='route')
        item['tunnel_id'] = 'tunnel_ab12_' + 'a' * 32
        self.assertEqual(parse_routes(self.config([item]))[0].listen_port, 1024)
        item['listen_port'] = 65535
        self.assertEqual(parse_routes(self.config([item]))[0].listen_port, 65535)

    def test_malformed_route_collections_and_entries_fail_closed(self):
        for value in (None, {}, '201', [None], [{}], [True],
                      [dict(route('201', 18770, 'a'), unknown=True)],
                      [dict(route('201', 18770, 'a'), __template_key='other')]):
            with self.subTest(value=value), self.assertRaises(RouteError):
                parse_routes(self.config(value))

    def test_route_count_limit_applies_to_otherwise_valid_distinct_routes(self):
        items = [{'sender_id': str(201 + index), 'listen_port': 18770 + index,
                  'tunnel_id': f'tunnel_{index:032x}'} for index in range(17)]
        self.assertEqual(len(parse_routes(self.config(items[:16]))), 16)
        with self.assertRaises(RouteError):
            parse_routes(self.config(items))

    def test_missing_required_route_fields_fail_closed(self):
        for key in ('sender_id', 'tunnel_id', 'listen_port'):
            item = route('201', 18770, 'a')
            del item[key]
            with self.subTest(key=key), self.assertRaises(RouteError):
                parse_routes(self.config([item]))

    def test_sender_ids_reject_bot_duplicate_noncanonical_and_path_values(self):
        for sender in (None, True, 201, '', '0', '01', '-1', '100', '201\n',
                       '201 ', '../201', '2' * 21):
            with self.subTest(sender=sender), self.assertRaises(RouteError):
                parse_routes(self.config([dict(route('201', 18770, 'a'), sender_id=sender)]))
        with self.assertRaises(RouteError):
            parse_routes(self.config([route('201', 18770, 'a'), route('201', 18771, 'b')]))

    def test_tunnel_ids_reject_duplicates_malformed_and_nonstring_values(self):
        for tunnel in (None, True, 123, '', 'tunnel_short', 'tunnel_' + 'A' * 32,
                       'tunnel_' + 'a' * 32 + '\n', 'https://example.test/tunnel'):
            with self.subTest(tunnel=tunnel), self.assertRaises(RouteError):
                parse_routes(self.config([dict(route('201', 18770, 'a'), tunnel_id=tunnel)]))
        with self.assertRaises(RouteError):
            parse_routes(self.config([route('201', 18770, 'a'), route('202', 18771, 'a')]))

    def test_ports_reject_duplicates_nonintegers_and_out_of_range_values(self):
        for port in (None, True, False, '18770', 18770.0, -1, 0, 1023, 65536):
            with self.subTest(port=port), self.assertRaises(RouteError):
                parse_routes(self.config([dict(route('201', 18770, 'a'), listen_port=port)]))
        with self.assertRaises(RouteError):
            parse_routes(self.config([route('201', 18770, 'a'), route('202', 18770, 'b')]))

    def test_one_invalid_route_rejects_whole_configuration(self):
        for bad in (dict(route('202', 18771, 'b'), sender_id='100'),
                    dict(route('202', 18771, 'b'), listen_port=True)):
            with self.assertRaises(RouteError):
                parse_routes(self.config([route('201', 18770, 'a'), bad]))


class SenderRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_main(self.temp.name)
        self.endpoints = []
        self.module.Endpoint = type('TestEndpoint', (RecordingEndpoint,),
                                    {'instances': self.endpoints})
        self.config = {
            'enabled': True, 'platform_id': 'instance-1', 'adapter': 'aiocqhttp',
            'self_id': '100', 'sender_id': '200', 'mcp_token': 'x' * 32,
            'access_rules_version': 1, 'private_sender_ids': ['201', '202', '299'],
            'group_rules': [group_rule()], 'tunnel_enabled': False,
            'sender_routes': [route('201', 18770, 'a'), route('202', 18771, 'b')],
        }
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def child(self, sender):
        return self.plugin.route_runtimes[sender]

    async def reload(self):
        await self.plugin.terminate()
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)

    @asynccontextmanager
    async def fake_tunnels(self):
        await self.plugin.terminate()
        self.config.update(tunnel_enabled=True, tunnel_id='',
                           tunnel_runtime_key='DUMMY_RUNTIME_KEY_' + 'y' * 40)
        supervisors = []
        fake = type('TestTunnelSupervisor', (RecordingTunnel,), {'instances': supervisors})
        tunnel_module = importlib.import_module('plugin_mock.tunnel_runtime')
        with patch.object(tunnel_module, 'TunnelSupervisor', fake), patch(
                'asyncio.create_subprocess_exec', new=AsyncMock(
                    side_effect=AssertionError('subprocess forbidden in synthetic route test'))) as spawn:
            try:
                await self.plugin.initialize()
                self.assertTrue(self.plugin.ready)
                yield supervisors
                spawn.assert_not_awaited()
            finally:
                for supervisor in supervisors:
                    supervisor.close_behavior = 'normal'
                await self.plugin.terminate()

    async def accept(self, event):
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        return next(reversed(self.child(event.sender).captured))

    async def reject(self, event):
        counts = {sender: child.host.store.status()['messages']
                  for sender, child in self.plugin.route_runtimes.items()}
        await self.plugin._on_message(event)
        self.assertFalse(event.stopped)
        self.assertEqual(counts, {
            sender: child.host.store.status()['messages']
            for sender, child in self.plugin.route_runtimes.items()})

    async def test_private_routes_have_distinct_stores_hosts_endpoints_and_roots(self):
        first, second = self.child('201'), self.child('202')
        self.assertIsNot(first.host, second.host)
        self.assertIsNot(first.host.store, second.host.store)
        self.assertIsNot(first.endpoint, second.endpoint)
        self.assertIsNot(first.group_candidates, second.group_candidates)
        self.assertIsNone(self.plugin.host)
        self.assertIsNone(self.plugin.endpoint)
        self.assertEqual(self.config['sender_id'], '200')
        root = Path(self.temp.name) / 'plugin_data' / 'astrbot_plugin_dot_private'
        for sender, child in self.plugin.route_runtimes.items():
            database = child.host.store.db.execute('PRAGMA database_list').fetchone()[2]
            self.assertEqual(Path(database), root / 'routes' / sender / 'inbox.sqlite3')
        self.assertEqual({item.listen_port for item in self.endpoints}, {18770, 18771})
        self.assertTrue(all(item.listen_host == '127.0.0.1' for item in self.endpoints))
        self.assertTrue(all(item.token == self.config['mcp_token'] for item in self.endpoints))
        self.assertTrue(all(item.started for item in self.endpoints))

    async def test_real_tunnel_setup_uses_distinct_routes_and_shared_global_key(self):
        async with self.fake_tunnels() as supervisors:
            self.assertEqual(len(supervisors), 2)
            root = Path(self.temp.name) / 'plugin_data' / 'astrbot_plugin_dot_private' / 'routes'
            for sender, port, marker, supervisor in zip(
                    ('201', '202'), (18770, 18771), ('a', 'b'), supervisors):
                self.assertIs(self.child(sender).tunnel, supervisor)
                self.assertEqual(supervisor.settings.tunnel_id, 'tunnel_' + marker * 32)
                self.assertEqual(supervisor.endpoint_url, f'http://127.0.0.1:{port}/mcp')
                self.assertEqual(supervisor.private, root / sender / 'tunnel-private')
                self.assertEqual(Path(supervisor.settings.runtime_key_file),
                                 supervisor.private / 'tunnel-runtime-key')
                self.assertEqual(supervisor.runtime_key, self.config['tunnel_runtime_key'])
                self.assertEqual(supervisor.mcp_token, self.config['mcp_token'])
                self.assertEqual(supervisor.private.stat().st_mode & 0o777, 0o700)
                self.assertEqual(Path(supervisor.settings.runtime_key_file).stat().st_mode & 0o777, 0o600)
            request = types.SimpleNamespace(username='local-admin',
                                            plugin_name='astrbot_plugin_dot_private')
            status = self.plugin.admin.status(request)
            routes = status['runtime']['routes']
            self.assertEqual({item['sender_id'] for item in routes}, {'201', '202'})
            self.assertEqual({item['listen_port'] for item in routes}, {18770, 18771})
            self.assertTrue(all(item['mcp_ready'] and item['tunnel']['process_running'] for item in routes))
            self.assertNotIn(self.config['tunnel_runtime_key'], json.dumps(status))
            self.assertNotIn(self.config['mcp_token'], json.dumps(status))

    async def test_incomplete_tunnel_close_retains_child_and_blocks_replacement(self):
        for behavior in ('raise', 'uncertain'):
            with self.subTest(close_behavior=behavior):
                async with self.fake_tunnels() as supervisors:
                    child = self.child('201')
                    host = child.host
                    supervisor = child.tunnel
                    supervisor.close_behavior = behavior
                    with self.assertRaisesRegex(RuntimeError, '^tunnel_stop_incomplete$'):
                        await self.plugin._stop_runtime()
                    self.assertFalse(self.plugin.ready)
                    self.assertEqual(set(self.plugin.route_runtimes), {'201'})
                    self.assertIs(self.child('201'), child)
                    self.assertIs(child.tunnel, supervisor)
                    self.assertEqual(child.runtime_error, 'tunnel_stop_incomplete')
                    self.assertIsNone(child.host)
                    self.assertTrue(host.stopping)
                    with self.assertRaises(sqlite3.Error):
                        host.store.status()
                    self.assertFalse(supervisors[1].running)
                    with self.assertRaisesRegex(RuntimeError, '^tunnel_stop_incomplete$'):
                        await self.plugin._start_enabled()
                    self.assertEqual(len(supervisors), 2)
                    supervisor.close_behavior = 'normal'
                    await self.plugin._stop_runtime()
                    self.assertFalse(supervisor.running)
                    self.assertIsNone(child.tunnel)
                    self.assertFalse(self.plugin.route_runtimes)
                    await self.plugin._start_enabled()
                    self.assertTrue(self.plugin.ready)
                    self.assertEqual(len(supervisors), 4)
                    self.assertTrue(all(not item.running for item in supervisors[:2]))

    async def test_admin_invalid_native_routes_are_sanitized(self):
        request = types.SimpleNamespace(username='local-admin',
                                        plugin_name='astrbot_plugin_dot_private')
        bad = 'synthetic-private-native-setting'
        self.config['sender_routes'] = [dict(route('201', 18770, 'a'), tunnel_id=bad)]
        status = self.plugin.admin.status(request)
        self.assertEqual(status['settings']['sender_routes'], [])
        self.assertEqual(status['runtime']['error_code'], 'invalid_sender_routes')
        self.assertNotIn(bad, json.dumps(status))
        self.assertTrue(all(not item['mcp_ready'] for item in status['runtime']['routes']))
        with self.assertRaisesRegex(self.module.AdminError, '^invalid_sender_routes$'):
            self.plugin.admin._validate({})

    async def test_admin_multi_route_validation_allows_blank_global_tunnel_id(self):
        validated = self.plugin.admin._validate({'tunnel_enabled': True, 'tunnel_id': ''})
        self.assertEqual(validated['tunnel_id'], '')
        self.assertEqual(validated['sender_routes'], self.config['sender_routes'])
        with self.assertRaisesRegex(self.module.AdminError, '^invalid_settings$'):
            self.plugin.admin._validate({'tunnel_enabled': True, 'tunnel_id': '', 'sender_routes': []})

    async def test_same_source_id_is_independent_for_different_senders(self):
        first, second = event_for('201'), event_for('202')
        first_mid, second_mid = await self.accept(first), await self.accept(second)
        self.assertNotEqual(first_mid, second_mid)
        for sender, mid in (('201', first_mid), ('202', second_mid)):
            child = self.child(sender)
            self.assertEqual(set(child.captured), {mid})
            self.assertEqual(child.host.store.read(mid)['sender_id'], sender)
            self.assertEqual(child.host.store.status()['messages'], 1)
        self.assertEqual(await self.accept(event_for('201')), first_mid)
        self.assertEqual(self.child('201').host.store.status()['messages'], 1)
        self.assertEqual(self.child('202').host.store.status()['messages'], 1)

    async def test_reply_stays_with_original_sender_and_reply_keys_are_independent(self):
        first, second = event_for('201'), event_for('202')
        first_mid, second_mid = await self.accept(first), await self.accept(second)
        result = await self.child('201').host.reply(first_mid, 'only first', 'same-key')
        self.assertEqual(result['state'], 'adapter_completed')
        self.assertEqual(first.sent[0][0].text, 'only first')
        self.assertEqual(second.sent, [])
        result = await self.child('202').host.reply(second_mid, 'only second', 'same-key')
        self.assertEqual(result['state'], 'adapter_completed')
        self.assertEqual(second.sent[0][0].text, 'only second')
        self.assertEqual(len(first.sent), 1)

    async def test_other_route_message_ids_cannot_be_read_validated_or_replied_to(self):
        events = [event_for('201'), event_for('202')]
        mids = [await self.accept(event) for event in events]
        for sender, foreign_mid in (('201', mids[1]), ('202', mids[0])):
            child = self.child(sender)
            with self.assertRaises(self.module.Rejected):
                child.host.store.read(foreign_mid)
            with self.assertRaises(self.module.Rejected):
                child._validate_read_message(foreign_mid)
            with self.assertRaises(self.module.Rejected):
                await child.host.reply(foreign_mid, 'must not send', 'foreign')
        self.assertEqual([event.sent for event in events], [[], []])

    async def test_unmapped_sender_rejected_even_when_private_permission_exists(self):
        await self.reject(event_for('299'))
        await self.reject(event_for('200'))
        await self.reject(event_for('299', group='300'))

    async def test_raw_sender_bot_route_and_source_mismatches_never_capture(self):
        for key, value in (('user_id', 202), ('sender', {'user_id': 202}),
                           ('self_id', 999), ('message_id', 'different'),
                           ('group_id', 300), ('user_id', True),
                           ('message_type', 'group')):
            with self.subTest(key=key, value=value):
                event = event_for('201')
                event.message_obj.raw_message[key] = value
                await self.reject(event)
        for sender in (True, None, '0', '01', '-1', '201\n'):
            with self.subTest(sender=sender):
                event = event_for('201')
                event.sender = sender
                await self.reject(event)

    async def test_bot_self_echo_never_captured_in_private_or_group(self):
        await self.reject(event_for('100'))
        await self.reject(event_for('100', group='300'))

    async def test_child_boundary_rejects_other_sender_even_without_parent_dispatch(self):
        for group in (None, '300'):
            event = event_for('202', group=group)
            await self.child('201')._on_message(event)
            self.assertFalse(event.stopped)
        self.assertEqual(self.child('201').host.store.status()['messages'], 0)

    async def test_all_sender_group_rule_still_routes_only_the_actual_sender(self):
        first, second = event_for('201', group='300'), event_for('202', group='300')
        first_mid, second_mid = await self.accept(first), await self.accept(second)
        for sender, mid in (('201', first_mid), ('202', second_mid)):
            row = self.child(sender).host.store.read(mid)
            self.assertEqual((row['sender_id'], row['group_id'], row['privacy_scope']),
                             (sender, '300', 'group_shared'))
        result = await self.child('202').host.reply(second_mid, 'group reply', 'group-key')
        self.assertEqual(result['state'], 'adapter_completed')
        self.assertEqual(first.sent, [])
        self.assertEqual(second.sent[0][0].text, 'group reply')
        await self.reject(event_for('201', group='999'))

    async def test_mapping_does_not_grant_private_or_selected_group_permission(self):
        self.config['private_sender_ids'] = ['201']
        self.config['group_rules'] = [group_rule(mode='selected', senders=['201'])]
        await self.reload()
        await self.accept(event_for('201'))
        await self.accept(event_for('201', group='300'))
        await self.reject(event_for('202'))
        await self.reject(event_for('202', group='300', mention=True))

    async def test_mapping_does_not_remove_real_mention_requirement(self):
        self.config['group_rules'] = [group_rule(mode='at')]
        await self.reload()
        await self.reject(event_for('201', group='300'))
        await self.accept(event_for('201', group='300', mention=True))
        await self.accept(event_for('202', group='300', mention=True))

    async def test_explicit_empty_private_permissions_still_deny_mapped_senders(self):
        self.config['private_sender_ids'] = []
        await self.reload()
        await self.reject(event_for('201'))
        await self.reject(event_for('202'))
        await self.accept(event_for('201', group='300'))

    async def test_private_permission_removal_revokes_existing_read_and_send(self):
        first, second = event_for('201'), event_for('202')
        first_mid, second_mid = await self.accept(first), await self.accept(second)
        self.config['private_sender_ids'] = ['202']
        child = self.child('201')
        with self.assertRaises(self.module.Rejected):
            child.host.store.read(first_mid)
        self.assertEqual((await child.host.reply(first_mid, 'revoked', 'revoke'))['state'], 'not_sent')
        self.assertEqual(first.sent, [])
        # Any unapplied configuration revision suspends the whole route set.
        with self.assertRaises(self.module.Rejected):
            self.child('202').host.store.read(second_mid)
        await self.reject(event_for('201', source='later'))
        await self.reload()
        self.assertEqual(self.child('202').host.store.read(second_mid)['sender_id'], '202')
        with self.assertRaises(self.module.Rejected):
            self.child('201').host.store.read(first_mid)

    async def test_group_permission_narrowing_revokes_other_mapped_sender(self):
        event = event_for('202', group='300')
        mid = await self.accept(event)
        self.config['group_rules'] = [group_rule(mode='selected', senders=['201'])]
        child = self.child('202')
        with self.assertRaises(self.module.Rejected):
            child.host.store.read(mid)
        self.assertEqual((await child.host.reply(mid, 'revoked', 'revoke-group'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_disable_after_awaited_preflight_prevents_any_child_send(self):
        event = event_for('201')
        mid = await self.accept(event)
        child = self.child('201')
        entered, release = asyncio.Event(), asyncio.Event()
        original = child.host.preflight

        async def preflight(row):
            result = await original(row)
            entered.set()
            await release.wait()
            return result

        child.host.preflight = preflight
        reply = asyncio.create_task(child.host.reply(mid, 'must not send', 'disabled'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.config['enabled'] = False
        finally:
            release.set()
        self.assertEqual((await reply)['state'], 'not_sent')
        self.assertEqual(event.sent, [])
        await self.reject(event_for('202'))

    async def test_route_edit_after_awaited_preflight_revokes_pending_reply(self):
        event = event_for('201')
        mid = await self.accept(event)
        child = self.child('201')
        entered, release = asyncio.Event(), asyncio.Event()
        original = child.host.preflight

        async def preflight(row):
            result = await original(row)
            entered.set()
            await release.wait()
            return result

        child.host.preflight = preflight
        reply = asyncio.create_task(child.host.reply(mid, 'must not send', 'route-edited'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.config['sender_routes'][0]['tunnel_id'] = 'tunnel_' + 'c' * 32
        finally:
            release.set()
        self.assertEqual((await reply)['state'], 'not_sent')
        self.assertEqual(event.sent, [])
        await self.reject(event_for('202'))

    async def test_route_edit_revokes_message_attachment_and_pending_media_guards(self):
        mid = await self.accept(event_for('201'))
        child = self.child('201')
        self.assertIsNone(child._validate_read_message(mid))
        stager = child.host.media_staging
        content = b'synthetic file bytes'
        upload = stager.begin(mid, 'media', 'file', 'test.txt', len(content),
                              hashlib.sha256(content).hexdigest())
        descriptor = Mock(side_effect=AssertionError('revoked read reached attachment lookup'))
        child.host.store.attachments.descriptor = descriptor
        self.config['sender_routes'][0]['listen_port'] = 18772
        with self.assertRaisesRegex(self.module.Rejected, '^binding_inactive$'):
            child._validate_read_message(mid)
        with self.assertRaisesRegex(self.module.Rejected, '^binding_inactive$'):
            await child._read_attachment(mid, 'synthetic-attachment')
        descriptor.assert_not_called()
        with self.assertRaisesRegex(self.module.Rejected, '^binding_inactive$'):
            stager.chunk(mid, 'media', upload['upload_id'], 0,
                         base64.b64encode(content).decode('ascii'))
        with self.assertRaisesRegex(self.module.Rejected, '^binding_inactive$'):
            await stager.commit(mid, 'media', upload['upload_id'])

    async def test_terminate_closes_every_endpoint_store_and_capture(self):
        await self.accept(event_for('201'))
        await self.accept(event_for('202'))
        children = list(self.plugin.route_runtimes.values())
        hosts = [child.host for child in children]
        await self.plugin.terminate()
        self.assertFalse(self.plugin.ready)
        self.assertTrue(all(endpoint.closed for endpoint in self.endpoints))
        for child, host in zip(children, hosts):
            self.assertFalse(child.ready)
            self.assertIsNone(child.host)
            self.assertEqual(child.captured, {})
            self.assertTrue(host.stopping)
            with self.assertRaises(sqlite3.Error):
                host.store.status()

    async def test_one_endpoint_close_failure_does_not_skip_other_route_cleanup(self):
        children = list(self.plugin.route_runtimes.values())
        hosts = [child.host for child in children]

        async def fail():
            raise RuntimeError('synthetic endpoint close failure')

        children[0].endpoint.close = fail
        await self.plugin.terminate()
        self.assertFalse(self.plugin.ready)
        self.assertTrue(self.endpoints[1].closed)
        for host in hosts:
            self.assertTrue(host.stopping)
            with self.assertRaises(sqlite3.Error):
                host.store.status()

    async def test_restart_preserves_separate_histories_without_stale_send_handles(self):
        first, second = event_for('201'), event_for('202')
        first_mid, second_mid = await self.accept(first), await self.accept(second)
        await self.plugin.terminate()
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        for sender, mid in (('201', first_mid), ('202', second_mid)):
            child = self.child(sender)
            self.assertEqual(child.host.store.read(mid)['sender_id'], sender)
            self.assertEqual(child.captured, {})
            self.assertEqual((await child.host.reply(mid, 'stale', 'old-handle'))['state'], 'not_sent')
        self.assertEqual(first.sent, [])
        self.assertEqual(second.sent, [])

    async def test_second_endpoint_start_failure_rolls_back_every_started_route(self):
        await self.plugin.terminate()
        self.endpoints.clear()

        class FailingEndpoint(RecordingEndpoint):
            instances = self.endpoints

            async def start(endpoint):
                if endpoint.listen_port == 18771:
                    raise RuntimeError('synthetic endpoint startup failure')
                await super().start()

        self.module.Endpoint = FailingEndpoint
        await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)
        self.assertEqual(self.plugin.runtime_error, 'startup_failed')
        self.assertEqual(len(self.endpoints), 2)
        self.assertTrue(all(endpoint.closed for endpoint in self.endpoints))
        for endpoint in self.endpoints:
            self.assertTrue(endpoint.host.stopping)
            with self.assertRaises(sqlite3.Error):
                endpoint.host.store.status()
        event = event_for('201')
        await self.plugin._on_message(event)
        self.assertFalse(event.stopped)

    async def test_cancelled_startup_closes_all_routes_including_in_progress_child(self):
        await self.plugin.terminate()
        self.endpoints.clear()
        entered = asyncio.Event()

        class BlockingEndpoint(RecordingEndpoint):
            instances = self.endpoints

            async def start(endpoint):
                if endpoint.listen_port == 18771:
                    entered.set()
                    await asyncio.Future()
                await super().start()

        self.module.Endpoint = BlockingEndpoint
        start = asyncio.create_task(self.plugin.initialize())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            start.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await start
        self.assertFalse(self.plugin.ready)
        self.assertEqual(len(self.endpoints), 2)
        self.assertTrue(all(endpoint.closed for endpoint in self.endpoints))
        for endpoint in self.endpoints:
            with self.assertRaises(sqlite3.Error):
                endpoint.host.store.status()

    async def test_empty_routes_retain_legacy_single_runtime_and_history_path(self):
        await self.plugin.terminate()
        self.config['sender_routes'] = []
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertIsNotNone(self.plugin.host)
        self.assertIsNotNone(self.plugin.endpoint)
        self.assertFalse(self.plugin.route_runtimes)
        event = event_for('201')
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        mid = next(iter(self.plugin.captured))
        self.assertEqual(self.plugin.host.store.read(mid)['sender_id'], '201')
        root = Path(self.temp.name) / 'plugin_data' / 'astrbot_plugin_dot_private'
        database = self.plugin.host.store.db.execute('PRAGMA database_list').fetchone()[2]
        self.assertEqual(Path(database), root / 'inbox.sqlite3')


if __name__ == '__main__':
    unittest.main()
