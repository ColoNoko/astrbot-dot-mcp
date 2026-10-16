"""Captured OneBot attachment resolution; fake bot APIs and downloads only."""
import asyncio
import importlib
import json
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_attachments import FakeTransport
from test_groups import group_event
from test_main import Event, Plain, load_main, logs
from test_media_snapshot import Image, File


HTTP = 'http://media.example.com/image?secret=captured-http-token'
HTTPS = 'https://media.example.com/image?secret=captured-https-token'
API_HTTPS = 'https://media.example.com/api%2Fimage?secret=api%2Ftoken'
TOKEN = '0123456789abcdef0123456789ABCDEF.jpg'


class AttachmentResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_main(self.temp.name)
        self.config = {'enabled': True, 'platform_id': 'instance-1', 'adapter': 'aiocqhttp',
                       'self_id': '100', 'sender_id': '200', 'group_ids': ['300', '400'],
                       'group_trigger_mode': 'all_messages', 'mcp_token': 'x' * 32}
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.plugin.media_types = {'image': Image, 'file': File}
        self.cache = self.plugin.host.store.attachments
        await self.cache.transport.close()
        self.transport = FakeTransport()
        self.cache.transport = self.transport
        self.errors = importlib.import_module('plugin_mock.attachments')
        self.transport.validate = self.errors.AttachmentTransport().validate
        self.directory = self.plugin.host.store.path.parent / 'attachments'
        self.sequence = 0

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    def event(self, data=None, *, kind='image', group=False):
        event = group_event() if group else Event()
        self.sequence += 1
        event.message_obj.message_id = 'attachment-' + str(self.sequence)
        event.message_obj.raw_message.update(
            message_id=event.message_obj.message_id,
            message=[{'type': 'text', 'data': {'text': 'inspect'}},
                     {'type': kind, 'data': data if data is not None else {'file': TOKEN, 'url': HTTP}}])
        event.messages = [Plain('inspect'), Image() if kind == 'image' else File()]
        event.get_self_id = lambda: '100'
        event.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={'url': API_HTTPS}))
        return event

    async def capture(self, event):
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        message_id = next(reversed(self.plugin.captured))
        attachment_id = self.cache.list(message_id)[0]['attachment_id']
        return message_id, attachment_id

    async def read(self, event):
        return await self.plugin._read_attachment(*await self.capture(event))

    async def assert_unfetched(self, event, code):
        with self.assertRaisesRegex(self.errors.AttachmentError, '^' + code + '$'):
            await self.read(event)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_safe_opaque_image_tokens_are_captured_without_coercion(self):
        for token in (TOKEN, *('A' * 32 + suffix for suffix in
                      ('.image', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.JPG'))):
            with self.subTest(token=token):
                event = self.event({'file': token, 'url': HTTP, 'file_id': 'wrong-image-id'})
                spec = self.plugin._snapshot(event)['attachments'][0]
                self.assertEqual(spec['segment_index'], 1)
                self.assertEqual(spec['file_id'], token)
                self.assertEqual(spec['url'], HTTP)

    async def test_unsafe_image_tokens_are_omitted_and_never_sent_to_api(self):
        unsafe = ('', '.', '..', '.env', 'config.json', 'name.jpg', '123', '_', '-', 'a' * 32,
                  'a' * 31 + '.jpg', 'a' * 33 + '.jpg', 'g' * 32 + '.jpg',
                  'a' * 32 + '.json', 'a' * 32 + '..jpg', 'a' * 32 + '.jpg.secret',
                  'x..png', '../secret', '/etc/passwd', 'path/to/image',
                  r'C:\secret.png', r'path\image', 'file:///etc/passwd', 'base64://secret',
                  'data:image/png;base64,secret', HTTP, HTTPS, 'x%2fsecret', 'x:secret',
                  'image name', 'image\nsecret', 'image\x7f', '\uff21.png', 'x' * 257,
                  123, True, None, {'path': '/secret'})
        for token in unsafe:
            with self.subTest(token=token):
                event = self.event({'file': token, 'url': HTTPS})
                self.assertEqual(self.plugin._snapshot(event)['attachments'][0]['file_id'], '')
                result = await self.read(event)
                self.assertFalse(result['isError'])
                event.bot.call_action.assert_not_called()
                self.assertEqual(self.transport.calls[-1], HTTPS)

    async def test_url_in_image_file_stays_url_and_never_becomes_api_identifier(self):
        event = self.event({'file': HTTPS})
        spec = self.plugin._snapshot(event)['attachments'][0]
        self.assertEqual((spec['file_id'], spec['url']), ('', HTTPS))
        await self.read(event)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [HTTPS])

    async def test_raw_http_image_resolves_to_api_https_without_rewriting(self):
        event = self.event()
        result = await self.read(event)
        event.bot.call_action.assert_awaited_once_with(action='get_image', file=TOKEN, self_id=100)
        self.assertEqual(self.transport.calls, [API_HTTPS])
        self.assertTrue(any(item['type'] == 'image' for item in result['content']))
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_image_without_url_resolves_to_api_https(self):
        event = self.event({'file': TOKEN})
        await self.read(event)
        event.bot.call_action.assert_awaited_once_with(action='get_image', file=TOKEN, self_id=100)
        self.assertEqual(self.transport.calls, [API_HTTPS])

    async def test_api_is_preferred_to_captured_https(self):
        event = self.event({'file': TOKEN, 'url': HTTPS})
        await self.read(event)
        event.bot.call_action.assert_awaited_once()
        self.assertEqual(self.transport.calls, [API_HTTPS])

    async def test_group_image_uses_captured_bot_and_exact_raw_self_id(self):
        event = self.event(group=True)
        event.message_obj.raw_message['self_id'] = '100'
        await self.read(event)
        event.bot.call_action.assert_awaited_once_with(action='get_image', file=TOKEN, self_id='100')

    async def test_private_and_group_file_urls_are_scoped_and_preferred_to_http(self):
        for group in (False, True):
            with self.subTest(group=group):
                event = self.event({'file_id': 'current-file-id', 'url': HTTP}, kind='file', group=group)
                await self.read(event)
                expected = {'action': 'get_private_file_url', 'file_id': 'current-file-id', 'self_id': 100}
                if group:
                    expected.update(action='get_group_file_url', group_id=300)
                event.bot.call_action.assert_awaited_once_with(**expected)
        self.assertEqual(self.transport.calls, [API_HTTPS, API_HTTPS])

    async def test_api_may_not_return_local_path_inline_data_or_http_url(self):
        for value, code in ((HTTP, 'attachment_https_required'),
                            ('/tmp/secret', 'attachment_url_invalid'),
                            ('file:///tmp/secret', 'attachment_url_invalid'),
                            ('base64://secret', 'attachment_url_invalid'),
                            ('data:image/png;base64,secret', 'attachment_url_invalid'),
                            ('https://user:secret@media.example.com/file', 'attachment_url_invalid')):
            with self.subTest(value=value):
                event = self.event({'file': TOKEN, 'url': HTTP})
                event.bot.call_action.return_value = {'url': value}
                await self.assert_unfetched(event, code)

    async def test_missing_api_url_never_reads_response_file_or_base64_fields(self):
        for value in ({'file': '/tmp/secret', 'base64': 'secret'}, {'url': None}, {'url': ''},
                      {'url': 123}, {'data': {'url': API_HTTPS}}, API_HTTPS, None):
            with self.subTest(value=value):
                event = self.event()
                event.bot.call_action.return_value = value
                await self.assert_unfetched(event, 'attachment_api_url_missing')

    async def test_unsupported_api_keeps_valid_captured_https_fallback(self):
        event = self.event({'file': TOKEN, 'url': HTTPS})
        event.bot.call_action.side_effect = NotImplementedError('secret adapter endpoint')
        result = await self.read(event)
        event.bot.call_action.assert_awaited_once()
        self.assertEqual(self.transport.calls, [HTTPS])
        self.assertNotIn('secret', json.dumps(result))

    async def test_missing_api_url_keeps_valid_captured_https_fallback(self):
        for value in ({'file': '/tmp/secret', 'base64': 'secret'}, {'url': None}, {'url': ''},
                      {'url': 123}, {'data': {'url': API_HTTPS}}, API_HTTPS, None):
            with self.subTest(value=value):
                event = self.event({'file': TOKEN, 'url': HTTPS})
                event.bot.call_action.return_value = value
                result = await self.read(event)
                event.bot.call_action.assert_awaited_once()
                self.assertEqual(self.transport.calls[-1], HTTPS)
                self.assertNotIn('secret', json.dumps(result))

    async def test_missing_api_keeps_valid_captured_https_fallback(self):
        for bot in (None, types.SimpleNamespace(), types.SimpleNamespace(call_action=None)):
            with self.subTest(bot=bot):
                event = self.event({'file': TOKEN, 'url': HTTPS})
                event.bot = bot
                await self.read(event)
        self.assertEqual(self.transport.calls, [HTTPS] * 3)

    async def test_no_api_never_falls_back_to_http(self):
        event = self.event()
        del event.bot
        await self.assert_unfetched(event, 'attachment_https_required')

    async def test_unsupported_api_http_fails_with_fixed_code_and_no_secret_log(self):
        before_logs = list(logs)
        event = self.event()
        event.bot.call_action.side_effect = RuntimeError('secret ' + HTTP + ' ' + TOKEN)
        await self.assert_unfetched(event, 'attachment_api_failed')
        self.assertEqual(logs, before_logs)

    async def test_missing_api_and_no_url_has_fixed_unavailable_code(self):
        event = self.event({'file': TOKEN})
        del event.bot
        await self.assert_unfetched(event, 'attachment_api_unavailable')

    async def test_unsafe_identifier_without_url_never_calls_api(self):
        event = self.event({'file': '/tmp/secret'})
        await self.assert_unfetched(event, 'attachment_source_unavailable')
        event.bot.call_action.assert_not_called()

    async def test_normalized_image_path_and_url_are_not_resolution_sources(self):
        event = self.event({'file': TOKEN})
        event.messages[1].path = '/etc/passwd'
        event.messages[1].url = 'https://wrong.example.com/secret'
        await self.read(event)
        event.bot.call_action.assert_awaited_once_with(action='get_image', file=TOKEN, self_id=100)
        self.assertEqual(self.transport.calls, [API_HTTPS])

    async def test_second_image_uses_its_own_segment_not_first_image(self):
        first_token, second_token = 'a' * 32 + '.image', 'b' * 32 + '.image'
        event = self.event({'file': first_token, 'url': HTTP})
        event.message_obj.raw_message['message'].append(
            {'type': 'image', 'data': {'file': second_token, 'url': HTTP}})
        event.messages.append(Image())
        message_id, _ = await self.capture(event)
        attachment_id = self.cache.list(message_id)[1]['attachment_id']
        await self.plugin._read_attachment(message_id, attachment_id)
        event.bot.call_action.assert_awaited_once_with(action='get_image', file=second_token, self_id=100)

    async def test_registered_descriptor_cannot_substitute_source_or_segment(self):
        for field, value in (('segment_index', 0), ('kind', 'file'), ('source_file_id', 'other.image'),
                             ('source_file_id', '/etc/passwd'), ('source_url', API_HTTPS)):
            with self.subTest(field=field):
                event = self.event()
                message_id, attachment_id = await self.capture(event)
                with self.cache.db:
                    self.cache.db.execute('UPDATE message_attachments SET ' + field + '=? WHERE id=?',
                                          (value, attachment_id))
                with self.assertRaisesRegex(self.errors.AttachmentError, '^attachment_source_unavailable$'):
                    await self.plugin._read_attachment(message_id, attachment_id)
                event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_attachment_from_another_message_is_not_resolved(self):
        first, second = self.event(), self.event()
        first_id, first_attachment = await self.capture(first)
        second_id, _ = await self.capture(second)
        self.assertNotEqual(first_id, second_id)
        with self.assertRaisesRegex(self.errors.AttachmentError, '^attachment_not_found$'):
            await self.plugin._read_attachment(second_id, first_attachment)
        first.bot.call_action.assert_not_called()
        second.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_mutation_before_api_prevents_call(self):
        event = self.event()
        message_id, attachment_id = await self.capture(event)
        event.message_obj.raw_message['message'][1]['data']['file'] = 'changed.image'
        with self.assertRaises(self.module.Rejected):
            await self.plugin._read_attachment(message_id, attachment_id)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_route_and_host_mutations_after_api_block_success_and_fallback(self):
        for change in ('enabled', 'binding', 'group', 'source', 'host', 'stopping', 'bot', 'capture'):
            for api_fails in (False, True):
                with self.subTest(change=change, api_fails=api_fails):
                    event = self.event({'file': TOKEN, 'url': HTTPS}, group=True)
                    original_host, original_bot = self.plugin.host, event.bot
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def action(**kwargs):
                        entered.set()
                        await release.wait()
                        if api_fails:
                            raise RuntimeError('secret API failure')
                        return {'url': API_HTTPS}
                    event.bot.call_action = AsyncMock(side_effect=action)
                    message_id, attachment_id = await self.capture(event)
                    task = asyncio.create_task(self.plugin._read_attachment(message_id, attachment_id))
                    await entered.wait()
                    if change == 'enabled': self.config['enabled'] = False
                    elif change == 'binding': self.config['self_id'] = '999'
                    elif change == 'group': self.config['group_ids'] = ['400']
                    elif change == 'source': event.message_obj.raw_message['message'][1]['data']['file'] = 'changed.image'
                    elif change == 'host': self.plugin.host = types.SimpleNamespace()
                    elif change == 'stopping': original_host.stopping = True
                    elif change == 'bot': event.bot = types.SimpleNamespace(call_action=AsyncMock())
                    elif change == 'capture': self.plugin.captured[message_id] = self.event(group=True)
                    release.set()
                    try:
                        with self.assertRaises(self.errors.AttachmentError):
                            await task
                        self.assertEqual(self.transport.calls, [])
                        self.assertEqual(list(self.directory.iterdir()), [])
                    finally:
                        self.config.update(enabled=True, self_id='100', group_ids=['300', '400'])
                        self.plugin.host = original_host
                        original_host.stopping = False
                        event.bot = original_bot

    async def test_cancellation_during_api_never_falls_back_or_leaves_fetching_state(self):
        event = self.event({'file': TOKEN, 'url': HTTPS})
        entered = asyncio.Event()
        async def action(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        event.bot.call_action = AsyncMock(side_effect=action)
        message_id, attachment_id = await self.capture(event)
        task = asyncio.create_task(self.plugin._read_attachment(message_id, attachment_id))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.cache.db.execute('SELECT state FROM message_attachments WHERE id=?',
                                               (attachment_id,)).fetchone()[0], 'registered')

    async def test_lock_wait_rechecks_route_before_entering_api(self):
        event = self.event()
        message_id, attachment_id = await self.capture(event)
        await self.cache._lock.acquire()
        task = asyncio.create_task(self.plugin._read_attachment(message_id, attachment_id))
        await asyncio.sleep(0)
        self.config['enabled'] = False
        self.cache._lock.release()
        with self.assertRaises(self.module.Rejected):
            await task
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_cached_image_rechecks_binding_without_repeating_api(self):
        event = self.event()
        message_id, attachment_id = await self.capture(event)
        await self.plugin._read_attachment(message_id, attachment_id)
        await self.plugin._read_attachment(message_id, attachment_id)
        event.bot.call_action.assert_awaited_once()
        self.assertEqual(self.transport.calls, [API_HTTPS])
        self.config['enabled'] = False
        with self.assertRaises(self.module.Rejected):
            await self.plugin._read_attachment(message_id, attachment_id)

    async def test_bot_mutation_during_dns_blocks_download(self):
        event = self.event()
        self.cache.transport = self.errors.AttachmentTransport()
        async def addresses(host):
            event.bot = types.SimpleNamespace(call_action=AsyncMock())
            return []
        with patch.object(self.cache.transport, '_addresses', side_effect=addresses), \
                patch.object(self.errors.aiohttp, 'ClientSession') as session:
            with self.assertRaises(self.errors.AttachmentError):
                await self.read(event)
            session.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_bot_mutation_after_download_blocks_returning_attachment(self):
        event = self.event()
        original_download = self.transport.download
        async def download(*args, **kwargs):
            await original_download(*args, **kwargs)
            event.bot = types.SimpleNamespace(call_action=AsyncMock())
        self.transport.download = download
        with self.assertRaisesRegex(self.module.Rejected, '^binding_inactive$'):
            await self.read(event)

    async def test_unsafe_api_url_does_not_disable_existing_safe_https_source(self):
        for value in (HTTP, '/tmp/secret', 'file:///tmp/secret', 'https://user:secret@media.example.com/file'):
            with self.subTest(value=value):
                event = self.event({'file': TOKEN, 'url': HTTPS})
                event.bot.call_action.return_value = {'url': value}
                await self.read(event)
                self.assertEqual(self.transport.calls[-1], HTTPS)

    async def test_http_file_api_diagnostic_contains_only_safe_shape(self):
        event = self.event({'file_id': 'captured-file'}, kind='file')
        event.bot.call_action.return_value = {'url': 'http://PRIVATE_USER:PRIVATE_PASSWORD@files.example.com:8080/PRIVATE_PATH?token=PRIVATE_QUERY'}
        with self.assertRaises(self.errors.AttachmentError) as raised:
            await self.read(event)
        self.assertEqual(raised.exception.code, 'attachment_https_required')
        self.assertEqual(raised.exception.source_diagnostic, {
            'source': 'adapter_api', 'scheme': 'http', 'hostname': 'files.example.com', 'port': 8080})
        self.assertNotIn('PRIVATE_', json.dumps(raised.exception.source_diagnostic))
        self.assertEqual(self.transport.calls, [])
        event.bot.call_action.assert_awaited_once_with(action='get_private_file_url', file_id='captured-file', self_id=100)

    async def test_native_reader_http_file_failure_survives_real_rpc(self):
        import aiohttp
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('plugin_mock.attachment_endpoint', Path(__file__).resolve().parents[1] / 'endpoint.py')
        endpoint_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(endpoint_module)
        endpoint = endpoint_module.Endpoint(self.plugin.host, 'x' * 32, ['callbacks.example.com'], port=0)
        await endpoint.start()
        try:
            event = self.event({'file_id': 'captured-file'}, kind='file')
            event.bot.call_action.return_value = {'url': 'http://SECRET_USER:SECRET_PASS@files.example.com:8080/SECRET_PATH?token=SECRET_QUERY'}
            mid, aid = await self.capture(event)
            async with aiohttp.ClientSession() as client:
                async with client.post('http://127.0.0.1:' + str(endpoint.port) + '/mcp',
                    headers={'Authorization': 'Bearer ' + 'x' * 32, 'MCP-Protocol-Version': endpoint_module.PROTOCOL_VERSION,
                             'Mcp-Method': 'tools/call'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'qq_read_attachment', 'arguments': {'message_id': mid, 'attachment_id': aid}}}) as response:
                    self.assertEqual(response.status, 200)
                    body = await response.json()
            result = body['result']
            self.assertTrue(result['isError'])
            failure = json.loads(result['content'][0]['text'])
            diagnostic = failure.pop('transport_diagnostic')
            self.assertEqual(diagnostic['outcome'], 'failed')
            self.assertEqual(failure, {'error': 'attachment_https_required',
                'source_diagnostic': {'source': 'adapter_api', 'scheme': 'http', 'hostname': 'files.example.com', 'port': 8080}})
            self.assertNotIn('SECRET_', json.dumps(body))
            self.assertEqual(self.transport.calls, [])
        finally:
            await endpoint.close()


if __name__ == '__main__':
    unittest.main()
