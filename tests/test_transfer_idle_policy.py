"""v0.4.15 payload-progress policy. Synthetic loopback TLS and fake grants only.

One production-budget test deliberately runs beyond 60 real seconds. Short
budgets test stalls, protocol trickles, cancellation and slot/cache cleanup.
"""
import asyncio
import shutil
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import test_attachments as fixtures
import test_download_progress as progress

module = fixtures.module
AttachmentError = module.AttachmentError


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for loopback TLS')
class TransferIdleWireTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(progress.DownloadWireProgressTests.setUpClass.__func__)
    asyncSetUp = fixtures.AttachmentStoreTests.asyncSetUp
    register = fixtures.AttachmentStoreTests.register
    proxy = progress.DownloadWireProgressTests.proxy
    close_proxy = progress.DownloadWireProgressTests.close_proxy
    assert_clean_failed_read = progress.DownloadWireProgressTests.assert_clean_failed_read
    assert_safe = progress.DownloadWireProgressTests.assert_safe
    cancel_task = progress.DownloadWireProgressTests.cancel_task

    async def test_real_progress_survives_sixty_seconds_with_production_budgets(self):
        chunks = [(0, fixtures.PNG)] + [(10.2, b'x')] * 6
        size = len(fixtures.PNG) + 6
        await self.proxy(chunks, length=size)
        aid = self.register(url=progress.URL)
        started = time.monotonic()
        await self.store.read(fixtures.MID, aid)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 61)
        diagnostic = self.store.status()['last_read']
        self.assertEqual(diagnostic['outcome'], 'succeeded')
        self.assertEqual(diagnostic['bytes_received'], size)
        self.assertGreaterEqual(diagnostic['elapsed_ms'], 61000)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assertEqual((len(self.connections), len(self.requests)), (1, 1))
        self.assertFalse(self.store._reservations)
        self.assertFalse(self.store._readers)
        self.assertEqual(self.store._slots._value, 2)
        self.assert_safe(diagnostic)

    async def test_header_trickles_cannot_extend_twenty_second_header_budget(self):
        fragments = [(0, b'HTTP/1.1 200 OK\r\nX-Test: ')] + [(.025, b'x')] * 20
        await self.proxy(length=1, raw_header_chunks=fragments)
        aid = self.register(url=progress.URL)
        with patch.object(module, 'DOWNLOAD_HEADERS_TIMEOUT', .11), \
                patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .07):
            started = time.monotonic()
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        self.assertGreaterEqual(time.monotonic() - started, .10)
        self.assertLess(time.monotonic() - started, .5)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(diagnostic['timeout_kind'], 'response_headers')
        self.assertEqual(diagnostic['stage'], 'request_or_response_headers')
        self.assertNotIn('bytes_received', diagnostic)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)

    async def test_chunk_framing_bytes_cannot_fake_payload_progress(self):
        fragments = [(0, b'1;')] + [(.025, b'x')] * 20
        await self.proxy([(0, b'a')], chunked=True, raw_body_chunks=fragments)
        aid = self.register(url=progress.URL)
        with patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .11):
            started = time.monotonic()
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        self.assertGreaterEqual(time.monotonic() - started, .10)
        self.assertLess(time.monotonic() - started, .5)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(diagnostic['timeout_kind'], 'read_idle')
        self.assertEqual(diagnostic['stage'], 'response_body')
        self.assertEqual(diagnostic['bytes_received'], 1)
        self.assertGreaterEqual(diagnostic['last_progress_gap_ms'], 95)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)

    async def test_no_first_body_byte_times_out_and_releases_slots(self):
        await self.proxy(length=30, stall=True)
        aid = self.register(url=progress.URL)
        with patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .08):
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(diagnostic['timeout_kind'], 'read_idle')
        self.assertEqual(diagnostic['bytes_received'], 0)
        self.assertGreaterEqual(diagnostic['last_progress_gap_ms'], 65)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_close_cancels_two_active_reads_and_a_queued_read(self):
        await self.proxy([(0, b'a')], length=30, stall=True)
        with self.db:
            self.db.execute('BEGIN')
            registered = self.store.register(fixtures.MID, [
                {'kind': 'file', 'segment_index': index, 'filename': 'synthetic.bin',
                 'url': progress.URL, 'file_id': ''} for index in range(3)])
        aids = [value['attachment_id'] for value in registered]
        tasks = [asyncio.create_task(self.store.read(fixtures.MID, aid)) for aid in aids]
        for task in tasks:
            self.addAsyncCleanup(self.cancel_task, task)
        async with asyncio.timeout(1):
            while len(self.requests) < 2 or len(self.store._reservations) < 2:
                await asyncio.sleep(.002)
        self.assertEqual(self.store._slots._value, 0)
        self.assertEqual(len(self.requests), 2)
        await self.store.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in results))
        self.assert_clean_failed_read()
        self.assertEqual(self.store._last_read.snapshot()['outcome'], 'cancelled')
        self.assertEqual(len(self.requests), 2)


class PrivateHTTPAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def grant(self, *, guard=None):
        transport = module.AttachmentTransport()
        self.addAsyncCleanup(transport.close)
        descriptor = {'id': 'att_' + '1' * 32, 'message_id': fixtures.MID,
                      'kind': 'file', 'file_id': 'SECRET_FILE_ID'}
        owner = types.SimpleNamespace(transport=transport, _check=lambda: None,
            _private_http_enabled=lambda: True, descriptor=lambda *_: descriptor)
        scope = module._PrivateHTTPRead(owner, descriptor, conversation_type='private',
            bot=types.SimpleNamespace(call_action=AsyncMock(return_value={
                'url': 'http://media.example.com/SECRET_PATH?token=SECRET_QUERY'})),
            self_id='123456', guard=guard or (lambda: None))
        self.addCleanup(scope.close)
        return transport, await scope.resolve(), scope

    async def download_with_chunks(self, transport, grant, chunks, consume):
        session, context = fixtures.fake_session()
        response = session.get.return_value.__aenter__.return_value
        response.content = types.SimpleNamespace(iter_chunked=lambda _size: chunks)
        with patch.object(transport, '_addresses', AsyncMock(return_value=[dict(fixtures.entry(), port=80)])), \
                patch.object(aiohttp, 'TCPConnector'), \
                patch.object(aiohttp, 'ClientSession', return_value=context):
            await transport.download(grant, consume)
        self.assertEqual(session.get.call_count, 1)

    async def test_admitted_progressing_http_read_outlives_thirty_seconds_but_cannot_restart(self):
        now = [100.0]
        guard = unittest.mock.Mock()
        with patch.object(module, 'time', types.SimpleNamespace(monotonic=lambda: now[0])):
            transport, grant, scope = await self.grant(guard=guard)
            async def chunks():
                for _ in range(5):
                    now[0] += 15
                    await asyncio.sleep(0)
                    yield b'x'
            received = []
            await self.download_with_chunks(transport, grant, chunks(), received.append)
            self.assertEqual(received, [b'x'] * 5)
            self.assertEqual(now[0], 175)
            self.assertGreater(guard.call_count, 7)
            self.assertLess(module._HTTP_GRANTS[grant]['expires'], now[0])
            with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
                await transport.download(grant, received.append)
            scope.close()
            with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
                module._http_grant(grant)

    async def test_unstarted_http_grant_still_expires_before_any_network_or_sink(self):
        now = [100.0]
        with patch.object(module, 'time', types.SimpleNamespace(monotonic=lambda: now[0])):
            transport, grant, _scope = await self.grant()
            now[0] += 30
            with patch.object(transport, '_download', AsyncMock()) as download:
                sink = unittest.mock.Mock()
                with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_expired'):
                    await transport.download(grant, sink)
            download.assert_not_called()
            sink.assert_not_called()
            self.assertFalse(module._HTTP_GRANTS[grant]['started'])

    async def test_live_http_revocation_still_stops_after_admission_window(self):
        for revoke in ('config', 'route', 'descriptor', 'transport', 'scope'):
            with self.subTest(revoke=revoke):
                now = [100.0]
                allowed = [True]
                def guard():
                    if not allowed[0]:
                        raise AttachmentError('attachment_source_unavailable')
                with patch.object(module, 'time', types.SimpleNamespace(monotonic=lambda: now[0])):
                    transport, grant, scope = await self.grant(guard=guard)
                    async def chunks():
                        yield b'a'
                        now[0] += 45
                        if revoke == 'config':
                            scope.owner._private_http_enabled = lambda: False
                        elif revoke == 'route':
                            allowed[0] = False
                        elif revoke == 'descriptor':
                            scope.owner.descriptor = lambda *_: {}
                        elif revoke == 'transport':
                            scope.owner.transport = object()
                        else:
                            scope.close()
                        await asyncio.sleep(0)
                        yield b'b'
                    received = []
                    with self.assertRaises(AttachmentError) as failure:
                        await self.download_with_chunks(transport, grant, chunks(), received.append)
                    self.assertNotEqual(failure.exception.code, 'attachment_http_grant_expired')
                    self.assertEqual(received, [b'a'])
                    scope.close()


if __name__ == '__main__':
    unittest.main()
