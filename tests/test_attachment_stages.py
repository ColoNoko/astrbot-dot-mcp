"""Safe per-read telemetry; fake APIs/DNS and synthetic loopback proxy only.

No real QQ messages, user image reads, external downloads or DNS requests.
"""
import asyncio
import json
import socket
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_attachments as fixtures

module = fixtures.module
AttachmentError = module.AttachmentError
AttachmentTransport = module.AttachmentTransport
ProxySettings = fixtures.proxy_module.ProxySettings
SECRET_URL = 'https://media.example.com/SECRET_PATH?token=SECRET_QUERY'


class AttachmentStageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentStoreTests.asyncSetUp
    register = fixtures.AttachmentStoreTests.register

    async def real_transport(self, *, google=False):
        await self.store.transport.close()
        proxy = ProxySettings(True, 'http://127.0.0.1:3128', 'SECRET_USER', 'SECRET_PASSWORD') if google else None
        self.transport = AttachmentTransport(proxy=proxy, attachment_dns='google' if google else 'system')
        self.store.transport = self.transport
        return self.transport

    def assert_safe(self, diagnostic):
        self.assertEqual(set(diagnostic), {'read_id', 'stage', 'outcome', 'elapsed_ms',
            'stage_elapsed_ms', 'attachment_dns', 'proxy_enabled', 'stages'})
        self.assertRegex(diagnostic['read_id'], r'^read_[a-f0-9]{32}$')
        self.assertLessEqual(len(diagnostic['stages']), 24)
        self.assertNotIn('SECRET_', json.dumps(diagnostic))
        for key in ('elapsed_ms', 'stage_elapsed_ms'):
            self.assertIs(type(diagnostic[key]), int)
            self.assertGreaterEqual(diagnostic[key], 0)
        for span in diagnostic['stages']:
            self.assertIn(span['stage'], module._READ_STAGES)
            self.assertGreaterEqual(span['elapsed_ms'], 0)
        self.assertNotIn('hostname', json.dumps(diagnostic))

    async def test_runtime_options_are_visible_before_first_read(self):
        await self.real_transport(google=True)
        status = self.store.status()
        self.assertEqual(status['attachment_dns'], 'google')
        self.assertTrue(status['proxy_enabled'])
        self.assertIsNone(status['last_read'])
        self.assertNotIn('SECRET_', json.dumps(status))
        self.assertEqual(module.NETWORK_TIMEOUT, 20)
        self.assertEqual(module.DNS_TIMEOUT, 5)

    async def test_api_timeout_is_distinct_and_cancellation_finishes_before_return(self):
        await self.real_transport()
        aid = self.register(url=SECRET_URL, file_id='SECRET_FILE_ID')
        cancelled = asyncio.Event()
        async def resolver(descriptor):
            self.assertEqual(descriptor['file_id'], 'SECRET_FILE_ID')
            module.note_attachment_api('get_image')
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(module, 'NETWORK_TIMEOUT', .035), \
                patch.object(self.transport, '_addresses', AsyncMock()) as dns:
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid, resolver)
        error = failure.exception
        self.assertEqual(error.code, 'attachment_timeout')
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(error.dns_diagnostic)
        diagnostic = error.transport_diagnostic
        self.assert_safe(diagnostic)
        self.assertEqual(diagnostic['stage'], 'adapter_api')
        self.assertEqual(diagnostic['outcome'], 'failed')
        # wait_for's deadline starts before scheduling the resolver task, so a
        # busy test loop can consume part of that budget before its API marker.
        self.assertGreaterEqual(diagnostic['elapsed_ms'], 25)
        self.assertLessEqual(diagnostic['stage_elapsed_ms'], diagnostic['elapsed_ms'])
        self.assertEqual(diagnostic['stages'][-1]['api_action'], 'get_image')
        self.assertEqual(diagnostic['stages'][-1]['outcome'], 'timeout')
        self.assertEqual(self.store.status()['last_read'], diagnostic)
        await asyncio.sleep(.01)
        self.assertEqual(self.store.status()['last_read'], diagnostic)
        dns.assert_not_awaited()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_api_failure_and_untrusted_labels_cannot_leak_data(self):
        aid = self.register(url=SECRET_URL, file_id='SECRET_FILE_ID', filename='SECRET_FILENAME')
        async def resolver(_descriptor):
            module.note_attachment_api('get_image')
            for invalid in (SECRET_URL, ['get_image'], {'SECRET_HEADER': 'SECRET_TOKEN'}, None):
                module.note_attachment_api(invalid)
                module.note_attachment_api('get_image', outcome=invalid)
            raise RuntimeError('SECRET_EXCEPTION SECRET_QQ_CONTENT')
        with self.assertRaises(AttachmentError) as failure:
            await self.store.read(fixtures.MID, aid, resolver)
        self.assertEqual(failure.exception.code, 'attachment_source_unavailable')
        diagnostic = failure.exception.transport_diagnostic
        self.assert_safe(diagnostic)
        self.assertEqual(diagnostic['stage'], 'adapter_api')
        self.assertEqual(diagnostic['stages'][-1]['api_action'], 'get_image')
        self.assertNotIn('SECRET_', json.dumps(self.store.status()))
        self.assertEqual(self.transport.calls, [])

    async def test_api_timeout_span_survives_successful_fallback(self):
        aid = self.register(url=SECRET_URL)
        async def resolver(_descriptor):
            module.note_attachment_api('get_image')
            await asyncio.sleep(.012)
            module.note_attachment_api('get_image', outcome='timeout')
            return SECRET_URL
        await self.store.read(fixtures.MID, aid, resolver)
        diagnostic = self.store.status()['last_read']
        self.assert_safe(diagnostic)
        api = next(span for span in diagnostic['stages'] if span['stage'] == 'adapter_api')
        self.assertEqual(api['outcome'], 'timeout')
        self.assertGreaterEqual(api['elapsed_ms'], 10)
        self.assertEqual(diagnostic['outcome'], 'succeeded')
        self.assertEqual(diagnostic['stage'], 'complete')

    async def test_system_dns_failure_has_both_safe_diagnostics(self):
        await self.real_transport()
        aid = self.register(url=SECRET_URL)
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                AsyncMock(side_effect=socket.gaierror(-2, 'SECRET_EXCEPTION'))), \
                patch.object(module.aiohttp, 'ClientSession') as session:
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        error = failure.exception
        self.assertEqual(error.code, 'attachment_dns_failed')
        self.assertEqual(error.dns_diagnostic['phase'], 'system_lookup')
        self.assertEqual(error.transport_diagnostic['stage'], 'dns')
        self.assertEqual(error.transport_diagnostic['attachment_dns'], 'system')
        self.assertFalse(error.transport_diagnostic['proxy_enabled'])
        self.assert_safe(error.transport_diagnostic)
        session.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_google_dns_timeout_cannot_be_mistaken_for_download_headers(self):
        await self.real_transport(google=True)
        aid = self.register(url=SECRET_URL)
        with patch.object(self.transport._google_resolver, 'resolve',
                AsyncMock(side_effect=asyncio.TimeoutError)), \
                patch.object(module.aiohttp, 'ClientSession') as session:
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(failure.exception.code, 'attachment_timeout')
        self.assertEqual(diagnostic['stage'], 'dns')
        self.assertEqual(diagnostic['attachment_dns'], 'google')
        self.assertTrue(diagnostic['proxy_enabled'])
        self.assert_safe(diagnostic)
        self.assertEqual(self.transport._dns_slots._value, 4)
        self.assertFalse(self.transport._lookups)
        session.assert_not_called()

    async def test_cache_hit_has_no_invented_api_dns_or_connection_stage(self):
        aid = self.register()
        await self.store.read(fixtures.MID, aid)
        first = self.store.status()['last_read']
        resolver = AsyncMock(side_effect=AssertionError('Must use cached bytes'))
        await self.store.read(fixtures.MID, aid, resolver)
        second = self.store.status()['last_read']
        self.assertNotEqual(first['read_id'], second['read_id'])
        self.assertEqual([span['stage'] for span in second['stages']],
                         ['queued', 'validation', 'cache_io', 'result', 'complete'])
        self.assertEqual(len(self.transport.calls), 1)
        resolver.assert_not_awaited()

    async def test_latest_attempt_cannot_be_overwritten_by_older_read_completion(self):
        aid = self.register()
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolver(_descriptor):
            module.note_attachment_api('get_image')
            entered.set()
            await release.wait()
            raise AttachmentError('attachment_api_failed')
        older = asyncio.create_task(self.store.read(fixtures.MID, aid, resolver))
        await entered.wait()
        older_id = self.store.status()['last_read']['read_id']
        newer = asyncio.create_task(self.store.read(fixtures.MID, aid))
        await asyncio.sleep(0)
        newer_id = self.store.status()['last_read']['read_id']
        self.assertNotEqual(newer_id, older_id)
        self.assertEqual(self.store.status()['last_read']['stage'], 'queued')
        newer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await newer
        latest = self.store.status()['last_read']
        release.set()
        with self.assertRaises(AttachmentError) as failure:
            await older
        self.assertEqual(failure.exception.transport_diagnostic['read_id'], older_id)
        self.assertEqual(self.store.status()['last_read'], latest)
        self.assertEqual(latest['outcome'], 'cancelled')
        self.assertIsNone(module._READ_DIAGNOSTIC.get())

    async def test_revocation_after_api_wait_blocks_download_and_exposes_no_source(self):
        aid = self.register(url=SECRET_URL)
        allowed = True
        def guard():
            if not allowed:
                raise AttachmentError('attachment_stopping')
        async def resolver(_descriptor):
            nonlocal allowed
            module.note_attachment_api('get_image')
            await asyncio.sleep(0)
            allowed = False
            return SECRET_URL
        with self.assertRaises(AttachmentError) as failure:
            await self.store.read(fixtures.MID, aid, resolver, guard=guard)
        self.assertEqual(failure.exception.code, 'attachment_stopping')
        self.assertEqual(failure.exception.transport_diagnostic['stage'], 'source_validation')
        self.assert_safe(failure.exception.transport_diagnostic)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_cancellation_close_and_late_callbacks_cannot_change_finished_snapshot(self):
        aid = self.register()
        entered, release = asyncio.Event(), asyncio.Event()
        saved = []
        async def resolver(_descriptor):
            module.note_attachment_api('get_image')
            saved.append(module._READ_DIAGNOSTIC.get())
            entered.set()
            await release.wait()
        task = asyncio.create_task(self.store.read(fixtures.MID, aid, resolver))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        snapshot = self.store.status()['last_read']
        self.assertEqual(snapshot['outcome'], 'cancelled')
        await self.store.close()
        saved[0].mark('response_body')
        release.set()
        self.assertEqual(saved[0].snapshot(), snapshot)
        self.assertIsNone(module._READ_DIAGNOSTIC.get())
        self.assertEqual(list(self.directory.iterdir()), [])
        with self.assertRaisesRegex(AttachmentError, '^attachment_stopping$'):
            self.store.status()

    async def test_status_is_bounded_and_returns_independent_copies(self):
        aid = self.register()
        async def resolver(_descriptor):
            for _ in range(60):
                module.note_attachment_api('get_image')
                module.note_attachment_api('get_image', outcome='failed')
            return fixtures.URL
        await self.store.read(fixtures.MID, aid, resolver)
        original = self.store.status()['last_read']
        self.assert_safe(original)
        self.assertEqual(len(original['stages']), 24)
        copied = self.store.status()['last_read']
        copied['stage'] = 'SECRET_MUTATION'
        copied['stages'][0]['stage'] = 'SECRET_MUTATION'
        self.assertEqual(self.store.status()['last_read'], original)
        self.assertLess(len(json.dumps(original)), 4000)


class AttachmentWireStageTests(unittest.IsolatedAsyncioTestCase):
    async def setup_transport(self, behavior):
        self.closed = asyncio.Event()
        self.requests, self.writers, self.handler_tasks = [], [], set()
        async def handler(reader, writer):
            task = asyncio.current_task()
            self.handler_tasks.add(task)
            self.writers.append(writer)
            try:
                self.requests.append(await reader.readuntil(b'\r\n\r\n'))
                if behavior == 'body':
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx')
                    await writer.drain()
                elif behavior == 'http_error':
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n'
                                 b'X-Secret: SECRET_HEADER\r\n\r\n')
                    await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                self.closed.set()
                self.handler_tasks.discard(task)
        self.server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        transport = AttachmentTransport(proxy=ProxySettings(
            True, 'http://127.0.0.1:' + str(port), 'SECRET_USER', 'SECRET_PASSWORD'))
        self.addAsyncCleanup(self.cleanup_transport, transport)
        return transport

    async def cleanup_transport(self, transport):
        await transport.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
        if self.handler_tasks:
            await asyncio.gather(*self.handler_tasks, return_exceptions=True)

    async def http_grant(self, transport):
        descriptor = {'id': 'att_' + '1' * 32, 'message_id': fixtures.MID,
                      'kind': 'file', 'file_id': 'SECRET_FILE_ID'}
        owner = types.SimpleNamespace(transport=transport, _check=lambda: None,
            _private_http_enabled=lambda: True, descriptor=lambda *_: descriptor)
        scope = module._PrivateHTTPRead(owner, descriptor, conversation_type='private',
            bot=types.SimpleNamespace(call_action=AsyncMock(return_value={
                'url': SECRET_URL.replace('https:', 'http:')})),
            self_id='123456', guard=lambda: None)
        self.addCleanup(scope.close)
        return await scope.resolve()

    async def assert_wire_failure(self, behavior, stage, *, https=False, code='attachment_timeout'):
        transport = await self.setup_transport(behavior)
        source = SECRET_URL if https else await self.http_grant(transport)
        entry = dict(fixtures.entry(), port=443 if https else 80)
        with patch.object(transport, '_system_addresses', AsyncMock(return_value=[entry])), \
                patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                             AsyncMock(side_effect=AssertionError('No real DNS'))), \
                patch.object(module, 'DOWNLOAD_CONNECT_TIMEOUT', .07), \
                patch.object(module, 'DOWNLOAD_HEADERS_TIMEOUT', .07), \
                patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .07):
            with self.assertRaises(AttachmentError) as failure:
                await transport.download(source, lambda _chunk: None)
        error = failure.exception
        self.assertEqual(error.code, code)
        diagnostic = error.transport_diagnostic
        self.assertEqual(diagnostic['stage'], stage)
        self.assertEqual(diagnostic['outcome'], 'failed')
        self.assertEqual(diagnostic['attachment_dns'], 'system')
        self.assertTrue(diagnostic['proxy_enabled'])
        if behavior in ('body', 'http_error'):
            self.assertEqual(diagnostic['response_status'], 200 if behavior == 'body' else 403)
        else:
            self.assertNotIn('response_status', diagnostic)
        self.assertNotIn('SECRET_', json.dumps(diagnostic))
        stages = [span['stage'] for span in diagnostic['stages']]
        self.assertIn('dns', stages)
        self.assertIn('connection_or_request', stages)
        self.assertEqual(len(self.requests), 1)
        await asyncio.wait_for(self.closed.wait(), 1)
        self.assertTrue(all(writer.is_closing() for writer in self.writers))
        self.assertIsNone(module._READ_DIAGNOSTIC.get())
        return stages

    async def test_real_proxy_connect_wait_is_not_reported_as_headers_or_tls_only(self):
        stages = await self.assert_wire_failure('connect', 'connection_or_request', https=True)
        self.assertTrue(self.requests[0].startswith(b'CONNECT '))
        self.assertNotIn('response_headers', stages)
        self.assertNotIn('request_or_response_headers', stages)
        self.assertNotIn('response_body', stages)

    async def test_real_delayed_response_headers_are_measured_separately(self):
        stages = await self.assert_wire_failure('headers', 'request_or_response_headers')
        self.assertTrue(self.requests[0].startswith(b'GET http://'))
        self.assertNotIn('response_headers', stages)
        self.assertNotIn('response_body', stages)

    async def test_real_stalled_body_is_measured_after_response_headers(self):
        stages = await self.assert_wire_failure('body', 'response_body')
        self.assertLess(stages.index('response_headers'), stages.index('response_body'))

    async def test_real_http_error_stays_at_headers_without_copying_header_values(self):
        stages = await self.assert_wire_failure('http_error', 'response_headers', code='attachment_http_failed')
        self.assertNotIn('response_body', stages)


if __name__ == '__main__':
    unittest.main()
