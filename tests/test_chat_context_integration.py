"""Current-message-only reads retain permission gates and local history storage."""
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

import aiohttp
import test_attachment_resolution as fixtures


class ChatContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = fixtures.AttachmentResolutionTests.asyncTearDown
    event = fixtures.AttachmentResolutionTests.event
    capture = fixtures.AttachmentResolutionTests.capture

    @asynccontextmanager
    async def rpc(self):
        spec = importlib.util.spec_from_file_location(
            'plugin_mock.chat_endpoint', Path(__file__).resolve().parents[1] / 'endpoint.py')
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        endpoint = ep.Endpoint(self.plugin.host, 'x' * 32, ['callbacks.example.com'], port=0)
        await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                async def call(name, args):
                    async with client.post(
                        f'http://127.0.0.1:{endpoint.port}/mcp', headers={
                            'Authorization': 'Bearer ' + 'x' * 32,
                            'MCP-Protocol-Version': ep.PROTOCOL_VERSION,
                            'Mcp-Method': 'tools/call'}, json={
                                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                'params': {'name': name, 'arguments': args}}) as response:
                        self.assertEqual(response.status, 200)
                        return (await response.json())['result']
                yield call
        finally:
            await endpoint.close()

    async def test_current_group_revocation_blocks_read_independent_of_retired_config(self):
        mid, _ = await self.capture(self.event(group=True))
        self.config['group_ids'] = []
        for value in (0, 8, 'INVALID_LEGACY_VALUE'):
            self.config['chat_context_messages'] = value
            with self.assertRaisesRegex(Exception, '^binding_inactive$'):
                self.plugin._validate_read_message(mid)
        async with self.rpc() as rpc:
            result = await rpc('qq_read_message', {'message_id': mid})
            self.assertTrue(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text'])['error'], 'binding_inactive')
            self.assertNotIn('inspect', json.dumps(result))

    async def test_current_bot_platform_owner_or_disabled_state_blocks_read(self):
        mid, _ = await self.capture(self.event())
        for key, value in [('self_id', '999'), ('platform_id', 'other'), ('sender_id', '999'),
                           ('enabled', False)]:
            old = self.config[key]
            self.config[key] = value
            with self.assertRaisesRegex(Exception, '^binding_inactive$'):
                self.plugin._validate_read_message(mid)
            self.config[key] = old
        self.plugin.host.stopping = True
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            self.plugin._validate_read_message(mid)
        self.plugin.host.stopping = False

    async def test_read_without_stale_reactive_handle_never_downloads_or_attaches_history(self):
        old, _ = await self.capture(self.event())
        current, _ = await self.capture(self.event())
        self.plugin.captured.clear()
        self.assertIsNone(self.plugin._validate_read_message(current))
        self.assertFalse(hasattr(self.plugin.host, 'read_context'))
        async with self.rpc() as rpc:
            result = await rpc('qq_read_message', {'message_id': current})
        self.assertFalse(result['isError'])
        self.assertNotIn('conversation_context', result['structuredContent'])
        self.assertNotIn(old, json.dumps(result))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.plugin.host.store.message(old)['completed_at'], 0)

    async def test_authenticated_read_ignores_old_eight_and_any_legacy_context_hook(self):
        old, _ = await self.capture(self.event())
        current, _ = await self.capture(self.event())
        self.plugin.host.read_context = Mock(side_effect=AssertionError('history must not be read'))
        async with self.rpc() as rpc:
            for value in (8, 0, 'INVALID_LEGACY_VALUE'):
                self.config['chat_context_messages'] = value
                result = await rpc('qq_read_message', {'message_id': current})
                self.assertFalse(result['isError'])
                value = result['structuredContent']
                self.assertEqual(value['message_id'], current)
                self.assertEqual(value['text'], 'inspect')
                self.assertEqual(len(value['attachments']), 1)
                self.assertNotIn('conversation_context', value)
                self.assertNotIn(old, json.dumps(result))
            status = (await rpc('qq_status', {}))['structuredContent']
            timing = next(x for x in status['message_latency']['recent']
                          if x['message_id'] == current)
            self.assertIsNotNone(timing['timestamps']['first_tool_read'])
        self.plugin.host.read_context.assert_not_called()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.plugin.host.store.message(old)['completed_at'], 0)

    async def test_owner_at_restriction_still_rejects_peer_current_message(self):
        owner, _ = await self.capture(self.event(group=True))
        value = self.plugin._snapshot(self.event(group=True))
        value.update(sender_id='201', text='OTHER_SENDER_HISTORY', source_id='other-sender')
        peer, _ = self.plugin.host.store.ingest(value)
        self.config['group_trigger_mode'] = 'owner_at'
        self.assertIsNone(self.plugin._validate_read_message(owner))
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            self.plugin._validate_read_message(peer)
        async with self.rpc() as rpc:
            allowed = await rpc('qq_read_message', {'message_id': owner})
            denied = await rpc('qq_read_message', {'message_id': peer})
        self.assertFalse(allowed['isError'])
        self.assertNotIn('OTHER_SENDER_HISTORY', json.dumps(allowed))
        self.assertTrue(denied['isError'])
        self.assertEqual(json.loads(denied['content'][0]['text'])['error'], 'binding_inactive')
        self.assertNotIn('OTHER_SENDER_HISTORY', json.dumps(denied))

    async def test_upgrade_preserves_sqlite_messages_replies_and_authenticated_history_viewer(self):
        old, _ = await self.capture(self.event())
        sent = await self.plugin.host.reply(old, 'SAVED_REPLY_BEFORE_UPGRADE', 'retained-reply')
        self.assertEqual(sent['state'], 'adapter_completed')
        current, _ = await self.capture(self.event())
        path = self.plugin.host.store.path
        binding = self.plugin.host.store.binding
        before_messages = [tuple(row) for row in self.plugin.host.store.db.execute(
            'SELECT * FROM messages ORDER BY id')]
        before_replies = [tuple(row) for row in self.plugin.host.store.db.execute(
            'SELECT * FROM replies ORDER BY reply_key')]
        self.config['chat_context_messages'] = 8
        await self.plugin._stop_runtime()
        await self.plugin._start_enabled()
        self.assertTrue(self.plugin.ready)
        self.assertEqual(self.plugin.host.store.path, path)
        self.assertEqual(self.plugin.host.store.binding, binding)
        self.assertEqual(self.config['chat_context_messages'], 8)
        self.assertEqual([tuple(row) for row in self.plugin.host.store.db.execute(
            'SELECT * FROM messages ORDER BY id')], before_messages)
        self.assertEqual([tuple(row) for row in self.plugin.host.store.db.execute(
            'SELECT * FROM replies ORDER BY reply_key')], before_replies)
        request = types.SimpleNamespace(username='local-admin', plugin_name='astrbot_plugin_dot_private')
        status = self.plugin.admin.status(request)
        history = self.plugin.admin.history(request, {
            'csrf_token': status['csrf_token'], 'action': 'messages',
            'conversation_type': 'private', 'group_id': '', 'before': None, 'limit': 50})
        self.assertEqual({item['message_id'] for item in history['messages']}, {old, current})
        old_message = next(item for item in history['messages'] if item['message_id'] == old)
        self.assertEqual(old_message['processing_state'], 'completed')
        self.assertEqual(old_message['replies'][0]['text'], 'SAVED_REPLY_BEFORE_UPGRADE')
        self.assertNotIn('reply_key', old_message['replies'][0])
        self.assertEqual(len(old_message['attachments']), 1)
        async with self.rpc() as rpc:
            result = await rpc('qq_read_message', {'message_id': current})
        self.assertFalse(result['isError'])
        self.assertNotIn('conversation_context', result['structuredContent'])
        self.assertNotIn('SAVED_REPLY_BEFORE_UPGRADE', json.dumps(result))
        self.assertNotIn(old, json.dumps(result))
