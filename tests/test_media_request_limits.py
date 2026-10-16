"""Bounded outbound RPC ingress on loopback only; QQ adapter calls are fake."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

import aiohttp
from multidict import CIMultiDict

import test_main as fixtures


class MediaRequestLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MainTests.asyncSetUp(self)
        spec = importlib.util.spec_from_file_location(
            'plugin_mock.request_limits_endpoint', Path(__file__).resolve().parents[1] / 'endpoint.py')
        self.ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ep)
        self.endpoint = self.ep.Endpoint(self.plugin.host, 'x' * 32, ['callbacks.example.com'], port=0)
        await self.endpoint.start()
        self.url = f'http://127.0.0.1:{self.endpoint.port}/mcp'
        self.client = aiohttp.ClientSession()
        self.headers = {'Authorization': 'Bearer ' + 'x' * 32,
                        'Content-Type': 'application/json',
                        'MCP-Protocol-Version': self.ep.PROTOCOL_VERSION,
                        'Mcp-Method': 'tools/call'}
        self.sent = []
        self.send_gate = None
        self.one_entered = asyncio.Event()
        self.two_entered = asyncio.Event()

        async def action(**kwargs):
            # Record only a digest and route, never a second retained payload.
            blob = kwargs['message'][0]['data']['file']
            self.assertTrue(blob.startswith('base64://'))
            decoded = base64.b64decode(blob[9:], validate=True)
            self.sent.append({'size': len(decoded), 'sha256': hashlib.sha256(decoded).hexdigest(),
                              'action': kwargs['action'], 'self_id': kwargs['self_id'],
                              'user_id': kwargs.get('user_id'), 'group_id': kwargs.get('group_id')})
            del decoded
            self.one_entered.set()
            if len(self.sent) >= 2:
                self.two_entered.set()
            if self.send_gate is not None:
                await self.send_gate.wait()
            return {'message_id': len(self.sent) + 1000}

        self.event = fixtures.Event()
        self.event.get_self_id = lambda: '100'
        self.event.bot = types.SimpleNamespace(call_action=action)
        await self.plugin._on_message(self.event)
        self.assertTrue(self.event.stopped)
        self.mid = next(reversed(self.plugin.captured))

    async def asyncTearDown(self):
        if self.send_gate is not None:
            self.send_gate.set()
        await self.client.close()
        await self.endpoint.close()
        await fixtures.MainTests.asyncTearDown(self)

    def body(self, data_base64, key='media'):
        return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'qq_reply_attachment', 'arguments': {
                    'message_id': self.mid, 'kind': 'file', 'filename': 'synthetic.bin',
                    'data_base64': data_base64, 'reply_key': key}}}

    async def post(self, body, *, headers=None):
        # aiohttp's file payload streams this known-size body in bounded chunks.
        with io.BytesIO(json.dumps(body, separators=(',', ':')).encode()) as stream:
            async with self.client.post(self.url, headers=headers or self.headers, data=stream) as response:
                return response.status, await response.json()

    async def test_exact_20mb_new_file_rpc_hash_once_and_plus_one_rejected(self):
        self.assertEqual(self.ep.MAX_MEDIA_BODY, 32 * 1024 * 1024)
        original = b'new synthetic file\x00' + b'x' * (20_000_000 - 19)
        self.assertEqual(len(original), 20_000_000)
        expected = hashlib.sha256(original).hexdigest()
        encoded = base64.b64encode(original).decode('ascii')
        del original
        self.assertEqual(len(encoded), 26_666_668)
        body = self.body(encoded)
        for _ in range(2):
            status, result = await self.post(body)
            self.assertEqual(status, 200)
            self.assertFalse(result['result']['isError'])
            self.assertEqual(result['result']['structuredContent']['state'], 'adapter_completed')
        self.assertEqual(self.sent, [{'size': 20_000_000, 'sha256': expected,
                                     'action': 'send_private_msg', 'self_id': 100,
                                     'user_id': 200, 'group_id': None}])
        # 20,000,000 has one padding character. Replacing it makes the same
        # encoded length represent 20,000,001 bytes, exercising decoded bounds.
        self.assertTrue(encoded.endswith('='))
        body['params']['arguments'].update(data_base64=encoded[:-1] + 'A', reply_key='over-limit')
        status, result = await self.post(body)
        self.assertEqual(status, 200)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(json.loads(result['result']['content'][0]['text'])['error'], 'media_too_large')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.endpoint._media_requests._value, 2)
        stored = self.plugin.host.store.db.execute('SELECT text FROM replies').fetchall()
        self.assertEqual(len(stored), 1)
        self.assertLess(len(stored[0][0]), 1024)
        self.assertIn(expected, stored[0][0])

    async def test_unknown_length_stream_has_actual_32mib_hard_cap(self):
        async def chunks():
            block = b' ' * (128 * 1024)
            for _ in range(self.ep.MAX_MEDIA_BODY // len(block)):
                yield block
            yield b' '
        async with self.client.post(self.url, headers=self.headers, data=chunks()) as response:
            self.assertNotIn('Content-Length', response.request_info.headers)
            self.assertEqual(response.request_info.headers.get('Transfer-Encoding'), 'chunked')
            self.assertEqual(response.status, 413)
            self.assertEqual(await response.json(), {'error': 'body_too_large'})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.endpoint._media_requests._value, 2)
        self.assertEqual(self.endpoint.http_rejections['body_too_large'], 1)

    async def test_other_methods_remain_at_64kib_even_with_media_capacity(self):
        self.assertEqual(self.ep.MAX_BODY, 65536)
        for method, params in (
            ('tools/call', {'name': 'qq_reply', 'arguments': {
                'message_id': self.mid, 'reply_key': 'oversized-text', 'text': 'x' * 65536}}),
            ('tools/list', {'_meta': {'padding': 'x' * 65536}}),
        ):
            body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
            headers = {**self.headers, 'Mcp-Method': method}
            status, result = await self.post(body, headers=headers)
            self.assertEqual((status, result), (413, {'error': 'body_too_large'}))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.endpoint._media_requests._value, 2)

    async def test_two_authenticated_large_slots_cover_send_busy_and_auth_first(self):
        self.send_gate = asyncio.Event()
        encoded = base64.b64encode(b'x' * 1200).decode('ascii')
        tasks = []
        with patch.object(self.ep, 'MAX_BODY', 1024), patch.object(self.ep, 'MAX_MEDIA_BODY', 4096):
            try:
                tasks = [asyncio.create_task(self.post(self.body(encoded, f'parallel-{i}'))) for i in range(2)]
                await asyncio.wait_for(self.two_entered.wait(), 2)
                self.assertEqual(self.endpoint._media_requests._value, 0)
                status, result = await self.post(self.body(encoded, 'third'))
                self.assertEqual((status, result), (503, {'error': 'busy'}))
                async def chunked_third():
                    yield json.dumps(self.body(encoded, 'chunked-third')).encode()
                async with self.client.post(self.url, headers=self.headers, data=chunked_third()) as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(await response.json(), {'error': 'busy'})
                bad_headers = {**self.headers, 'Authorization': 'Bearer wrong'}
                status, result = await self.post(self.body(encoded, 'unauthorized'), headers=bad_headers)
                self.assertEqual((status, result), (401, {'error': 'unauthorized'}))
                # Ordinary small work remains available while both large sends await.
                status, result = await self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'qq_status', 'arguments': {}}})
                self.assertEqual(status, 200)
                self.assertFalse(result['result']['isError'])
                self.assertEqual(self.endpoint._media_requests._value, 0)
            finally:
                self.send_gate.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            self.assertIsInstance(result, tuple, result)
            self.assertEqual(result[0], 200)
            self.assertEqual(result[1]['result']['structuredContent']['state'], 'adapter_completed')
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.endpoint._media_requests._value, 2)

    def held_request(self, *, known_length):
        entered = asyncio.Event()
        class Content:
            async def iter_chunked(self, _size):
                if not known_length:
                    yield b' ' * 2048  # Acquire the slot after crossing the cap.
                entered.set()
                await asyncio.Event().wait()
                yield b''
        request = types.SimpleNamespace(headers=CIMultiDict(self.headers), path='/mcp',
            query_string='', method='POST', content_type='application/json',
            content_length=2048 if known_length else None, content=Content())
        return request, entered

    async def test_cancel_and_read_timeout_release_known_and_chunked_slots(self):
        with patch.object(self.ep, 'MAX_BODY', 1024), patch.object(self.ep, 'MAX_MEDIA_BODY', 4096):
            for known in (True, False):
                request, entered = self.held_request(known_length=known)
                task = asyncio.create_task(self.endpoint._handle(request))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(self.endpoint._media_requests._value, 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.endpoint._media_requests._value, 2)
                self.assertNotIn(task, self.endpoint._requests)
                request, entered = self.held_request(known_length=known)
                with patch.object(self.ep, 'READ_TIMEOUT', 0.02):
                    response = await self.endpoint._handle(request)
                self.assertEqual(response.status, 408)
                self.assertEqual(json.loads(response.body), {'error': 'request_timeout'})
                self.assertEqual(self.endpoint._media_requests._value, 2)
        self.assertEqual(self.sent, [])

    async def test_cancel_during_adapter_releases_slot_and_keeps_uncertain_dedupe(self):
        self.send_gate = asyncio.Event()
        body = self.body(base64.b64encode(b'x' * 1200).decode(), 'cancelled-send')
        raw = json.dumps(body).encode()
        class Content:
            async def iter_chunked(self, _size):
                yield raw
        request = types.SimpleNamespace(headers=CIMultiDict(self.headers), path='/mcp',
            query_string='', method='POST', content_type='application/json',
            content_length=len(raw), content=Content())
        with patch.object(self.ep, 'MAX_BODY', 1024), patch.object(self.ep, 'MAX_MEDIA_BODY', 4096):
            task = asyncio.create_task(self.endpoint._handle(request))
            await asyncio.wait_for(self.one_entered.wait(), 1)
            self.assertEqual(self.endpoint._media_requests._value, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.endpoint._media_requests._value, 2)
            self.assertNotIn(task, self.endpoint._requests)
            status, result = await self.post(body)
            self.assertEqual(status, 200)
            self.assertEqual(result['result']['structuredContent']['state'], 'uncertain')
        self.assertEqual(len(self.sent), 1)

    async def test_malformed_large_json_releases_slot(self):
        with patch.object(self.ep, 'MAX_BODY', 1024), patch.object(self.ep, 'MAX_MEDIA_BODY', 4096):
            async with self.client.post(self.url, headers=self.headers, data=b'{' + b' ' * 2048) as response:
                self.assertEqual(response.status, 413)
                self.assertEqual(await response.json(), {'error': 'body_too_large'})
        self.assertEqual(self.endpoint._media_requests._value, 2)
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
