"""Download progress and phase-timeout regressions; synthetic loopback TLS only.

No captured/user attachments, public sockets, real DNS, or external services.
Short budgets make failure tests fast. Production-duration idle-policy coverage
lives in test_transfer_idle_policy.py.
"""
import asyncio
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import test_attachments as fixtures

module = fixtures.module
AttachmentError = module.AttachmentError
AttachmentTransport = module.AttachmentTransport
ProxySettings = fixtures.proxy_module.ProxySettings
URL = 'https://media.example.com/SECRET_PATH?token=SECRET_QUERY'


class DownloadDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_preserve_api_dns_size_and_separate_download_budgets(self):
        self.assertEqual(module.NETWORK_TIMEOUT, 20)
        self.assertEqual(module.DNS_TIMEOUT, 5)
        self.assertFalse(hasattr(module, 'DOWNLOAD_TOTAL_TIMEOUT'))
        self.assertEqual(module.DOWNLOAD_HEADERS_TIMEOUT, 20)
        self.assertEqual(module.DOWNLOAD_CONNECT_TIMEOUT, 10)
        self.assertEqual(module.DOWNLOAD_READ_IDLE_TIMEOUT, 20)
        self.assertEqual(module.MAX_FILE_BYTES, 20_000_000)
        self.assertEqual(module.HTTP_GRANT_SECONDS, 30)

    def test_progress_fields_are_bounded_optional_numeric_and_freeze(self):
        diagnostic = module._ReadDiagnostic(None)
        for value in (None, True, -1, 2**63, 'SECRET_HEADER', {'secret': 1}):
            diagnostic.note_content_length(value)
            self.assertNotIn('content_length_bytes', diagnostic.snapshot())
        diagnostic.note_content_length(2**63 - 1)
        self.assertEqual(diagnostic.snapshot()['content_length_bytes'], 2**63 - 1)
        diagnostic.start_body()
        self.assertEqual(diagnostic.snapshot()['bytes_received'], 0)
        for value in (None, True, 0, -1, 'SECRET_BODY', {'secret': 1}):
            diagnostic.note_received(value)
        diagnostic.note_received(2**63)
        self.assertEqual(diagnostic.snapshot()['bytes_received'], 2**63 - 1)
        diagnostic.finish_download()
        diagnostic.finish('cancelled')
        snapshot = diagnostic.snapshot()
        diagnostic.note_content_length(1)
        diagnostic.note_received(1)
        self.assertEqual(diagnostic.snapshot(), snapshot)
        self.assertNotIn('SECRET_', json.dumps(snapshot))
        self.assertNotIn('timeout_kind', snapshot)

    async def test_untyped_timeout_is_not_guessed_from_its_text_or_stage(self):
        transport = AttachmentTransport()
        self.addAsyncCleanup(transport.close)
        async def fail(*_args, **_kwargs):
            module._attachment_stage('response_body')
            diagnostic = module._READ_DIAGNOSTIC.get()
            diagnostic.start_body()
            diagnostic.note_received(3)
            raise asyncio.TimeoutError('SECRET_TOKEN connect read idle total')
        with patch.object(transport, '_download', fail):
            with self.assertRaises(AttachmentError) as failure:
                await transport.download(URL, lambda _chunk: None)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(failure.exception.code, 'attachment_timeout')
        self.assertEqual(diagnostic['bytes_received'], 3)
        self.assertEqual(diagnostic['stage'], 'response_body')
        self.assertNotIn('timeout_kind', diagnostic)
        self.assertNotIn('SECRET_', json.dumps(diagnostic))

    async def test_dns_keeps_its_independent_budget_and_awaits_cancellation(self):
        transport = AttachmentTransport(attachment_dns='google',
            proxy=ProxySettings(True, 'http://127.0.0.1:3128'))
        self.addAsyncCleanup(transport.close)
        cancelled = asyncio.Event()
        async def delayed_dns(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(transport._google_resolver, 'resolve', delayed_dns), \
                patch.object(module, 'DNS_TIMEOUT', .05), \
                patch.object(aiohttp, 'ClientSession') as session:
            started = time.monotonic()
            with self.assertRaises(AttachmentError) as failure:
                await transport.download(URL, lambda _chunk: None)
        diagnostic = failure.exception.transport_diagnostic
        self.assertGreaterEqual(time.monotonic() - started, .04)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assertEqual(diagnostic['stage'], 'dns')
        self.assertEqual(failure.exception.dns_diagnostic['reason'], 'timeout')
        self.assertNotIn('bytes_received', diagnostic)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(transport._lookups)
        session.assert_not_called()


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for loopback TLS')
class DownloadWireProgressTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentStoreTests.asyncSetUp
    register = fixtures.AttachmentStoreTests.register

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix='attachment-progress-tls-')
        cls.addClassCleanup(directory.cleanup)
        cls.cert = Path(directory.name) / 'cert.pem'
        key = Path(directory.name) / 'key.pem'
        subprocess.run([shutil.which('openssl'), 'req', '-x509', '-newkey', 'rsa:2048',
            '-nodes', '-keyout', str(key), '-out', str(cls.cert), '-days', '1',
            '-subj', '/CN=media.example.com', '-addext', 'subjectAltName=DNS:media.example.com'],
            check=True, capture_output=True, timeout=20)
        cls.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls_context.load_cert_chain(cls.cert, key)

    async def proxy(self, chunks=(), *, length=None, stall=False, connect_stall=False,
                    headers_stall=False, forever=False, chunked=False,
                    raw_header_chunks=(), raw_body_chunks=()):
        await self.store.transport.close()
        self.connections, self.requests, self.errors = [], [], []
        self.writers, self.handlers = set(), set()
        self.closed, self.sent = asyncio.Event(), asyncio.Event()
        async def handler(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            self.writers.add(writer)
            try:
                self.connections.append(await reader.readuntil(b'\r\n\r\n'))
                if connect_stall:
                    await reader.read()
                    return
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                await writer.start_tls(self.tls_context, ssl_handshake_timeout=2)
                self.requests.append(await reader.readuntil(b'\r\n\r\n'))
                for delay, data in raw_header_chunks:
                    await asyncio.sleep(delay)
                    writer.write(data)
                    await writer.drain()
                if headers_stall:
                    await reader.read()
                    return
                header = (b'Transfer-Encoding: chunked\r\n' if chunked else
                          b'Content-Length: ' + str(length).encode() + b'\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\n' + header +
                             b'X-Secret: SECRET_HEADER\r\nConnection: close\r\n\r\n')
                await writer.drain()
                for delay, data in chunks:
                    if delay:
                        await asyncio.sleep(delay)
                    writer.write((format(len(data), 'x').encode() + b'\r\n' + data + b'\r\n')
                                 if chunked else data)
                    await writer.drain()
                    self.sent.set()
                for delay, data in raw_body_chunks:
                    await asyncio.sleep(delay)
                    writer.write(data)
                    await writer.drain()
                if forever:
                    while not reader.at_eof():
                        await asyncio.sleep(.025)
                        writer.write(b'x')
                        await writer.drain()
                elif stall:
                    await reader.read()
                elif chunked:
                    writer.write(b'0\r\n\r\n')
                    await writer.drain()
            except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
                pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors.append(type(error).__name__)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, ssl.SSLError):
                    pass
                self.writers.discard(writer)
                self.handlers.discard(task)
                self.closed.set()
        self.server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        transport = AttachmentTransport(proxy=ProxySettings(
            True, 'http://127.0.0.1:' + str(port), 'SECRET_USER', 'SECRET_PASSWORD'))
        self.transport = self.store.transport = transport
        self.addAsyncCleanup(self.close_proxy)
        verified = ssl.create_default_context(cafile=str(self.cert))
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(verified.check_hostname)
        self.enterContext(patch.object(aiohttp.connector, '_SSL_CONTEXT_VERIFIED', verified))
        self.enterContext(patch.object(transport, '_system_addresses', AsyncMock(return_value=[fixtures.entry()])))
        self.enterContext(patch.object(asyncio.get_running_loop(), 'getaddrinfo',
            AsyncMock(side_effect=AssertionError('No public DNS or sockets'))))
        return transport

    async def close_proxy(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*tuple(self.handlers), return_exceptions=True)
        self.assertFalse(self.writers)
        self.assertFalse(self.errors)

    def assert_clean_failed_read(self):
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(self.store._reservations)
        self.assertFalse(self.store._readers)
        self.assertEqual(self.store._slots._value, 2)
        self.assertIsNone(module._READ_DIAGNOSTIC.get())

    def assert_safe(self, diagnostic):
        serialized = json.dumps(diagnostic)
        for forbidden in ('SECRET_', 'media.example.com', '127.0.0.1', 'http://', 'https://'):
            self.assertNotIn(forbidden, serialized)
        for key in ('content_length_bytes', 'bytes_received', 'last_progress_gap_ms'):
            if key in diagnostic:
                self.assertIs(type(diagnostic[key]), int)
                self.assertGreaterEqual(diagnostic[key], 0)
        self.assertLess(len(serialized), 4000)

    async def test_real_https_steady_progress_completes_once_with_safe_diagnostics(self):
        chunks = [(0, fixtures.PNG)] + [(.01, b'x') for _ in range(5)]
        size = len(fixtures.PNG) + 5
        await self.proxy(chunks, length=size)
        aid = self.register(url=URL)
        started = time.monotonic()
        await self.store.read(fixtures.MID, aid)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, .04)
        self.assertLess(elapsed, 2)
        diagnostic = self.store.status()['last_read']
        self.assertEqual(diagnostic['outcome'], 'succeeded')
        self.assertEqual(diagnostic['response_status'], 200)
        self.assertEqual(diagnostic['content_length_bytes'], size)
        self.assertEqual(diagnostic['bytes_received'], size)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assertEqual((len(self.connections), len(self.requests)), (1, 1))
        self.assertTrue(self.connections[0].startswith(b'CONNECT 8.8.8.8:443 '))
        self.assertTrue(self.requests[0].startswith(b'GET /SECRET_PATH?token=SECRET_QUERY '))
        self.assert_safe(diagnostic)

    async def test_real_body_stall_reports_read_idle_and_preserves_received_bytes(self):
        await self.proxy([(0, b'xyz')], length=30, stall=True)
        aid = self.register(url=URL)
        with patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .08):
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(failure.exception.code, 'attachment_timeout')
        self.assertEqual(diagnostic['timeout_kind'], 'read_idle')
        self.assertEqual(diagnostic['stage'], 'response_body')
        self.assertEqual(diagnostic['content_length_bytes'], 30)
        self.assertEqual(diagnostic['bytes_received'], 3)
        self.assertGreaterEqual(diagnostic['last_progress_gap_ms'], 65)
        self.assertEqual((len(self.connections), len(self.requests)), (1, 1))
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_real_live_progress_counts_bytes_resets_gap_and_freezes_at_completion(self):
        await self.proxy([(0, b'a'), (.15, b'bc'), (.15, b'd')], length=4)
        aid = self.register(url=URL)
        task = asyncio.create_task(self.store.read(fixtures.MID, aid))
        self.addAsyncCleanup(self.cancel_task, task)
        async with asyncio.timeout(1):
            while self.store.status()['last_read'] is None or self.store.status()['last_read'].get('bytes_received') != 1:
                await asyncio.sleep(.002)
            await asyncio.sleep(.05)
            first = self.store.status()['last_read']
            self.assertEqual(first['bytes_received'], 1)
            self.assertGreaterEqual(first['last_progress_gap_ms'], 40)
            while self.store.status()['last_read'].get('bytes_received') != 3:
                await asyncio.sleep(.002)
            second = self.store.status()['last_read']
        self.assertLess(second['last_progress_gap_ms'], first['last_progress_gap_ms'])
        await task
        final = self.store.status()['last_read']
        self.assertEqual(final['bytes_received'], 4)
        self.assertEqual(final['content_length_bytes'], 4)
        self.assertEqual(final['outcome'], 'succeeded')
        await asyncio.sleep(.025)
        self.assertEqual(self.store.status()['last_read'], final)
        self.assert_safe(final)

    async def test_real_progress_can_extend_well_beyond_one_idle_interval(self):
        await self.proxy([(0, b'x')] + [(.03, b'x')] * 12, length=13)
        aid = self.register(url=URL)
        with patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .12):
            started = time.monotonic()
            await self.store.read(fixtures.MID, aid)
        elapsed = time.monotonic() - started
        diagnostic = self.store.status()['last_read']
        self.assertGreaterEqual(elapsed, .34)
        self.assertEqual(diagnostic['outcome'], 'succeeded')
        self.assertEqual(diagnostic['bytes_received'], 13)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assert_safe(diagnostic)

    async def test_real_connect_stall_is_classified_from_aiohttp_connection_timeout(self):
        await self.proxy(connect_stall=True)
        aid = self.register(url=URL)
        with patch.object(module, 'DOWNLOAD_CONNECT_TIMEOUT', .08):
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(diagnostic['timeout_kind'], 'connect')
        self.assertEqual(diagnostic['stage'], 'connection_or_request')
        self.assertNotIn('bytes_received', diagnostic)
        self.assertNotIn('response_status', diagnostic)
        self.assertEqual((len(self.connections), len(self.requests)), (1, 0))
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_real_header_stall_is_read_idle_without_invented_body_progress(self):
        await self.proxy(headers_stall=True)
        aid = self.register(url=URL)
        with patch.object(module, 'DOWNLOAD_READ_IDLE_TIMEOUT', .08):
            with self.assertRaises(AttachmentError) as failure:
                await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(diagnostic['timeout_kind'], 'read_idle')
        self.assertEqual(diagnostic['stage'], 'request_or_response_headers')
        self.assertNotIn('bytes_received', diagnostic)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_real_cancellation_closes_socket_cleans_cache_and_freezes_live_progress(self):
        await self.proxy([(0, b'xyz')], length=30, stall=True)
        aid = self.register(url=URL)
        task = asyncio.create_task(self.store.read(fixtures.MID, aid))
        self.addAsyncCleanup(self.cancel_task, task)
        async with asyncio.timeout(1):
            while self.store.status()['last_read'] is None or self.store.status()['last_read'].get('bytes_received') != 3:
                await asyncio.sleep(.002)
        before = self.store.status()['last_read']
        await asyncio.sleep(.025)
        after = self.store.status()['last_read']
        self.assertGreater(after['last_progress_gap_ms'], before['last_progress_gap_ms'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        diagnostic = self.store.status()['last_read']
        self.assertEqual(diagnostic['outcome'], 'cancelled')
        self.assertEqual(diagnostic['bytes_received'], 3)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assert_clean_failed_read()
        await asyncio.wait_for(self.closed.wait(), 1)
        await asyncio.sleep(.02)
        self.assertEqual(self.store.status()['last_read'], diagnostic)
        self.assert_safe(diagnostic)

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_real_chunked_stream_enforces_unchanged_twenty_mb_cap(self):
        size = module.MAX_FILE_BYTES + 1
        block = b'x' * 65536
        chunks = [(0, block)] * (size // len(block)) + [(0, b'x' * (size % len(block)))]
        await self.proxy(chunks, chunked=True)
        aid = self.register(url=URL)
        with self.assertRaises(AttachmentError) as failure:
            await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(failure.exception.code, 'attachment_too_large')
        self.assertEqual(diagnostic['bytes_received'], size)
        self.assertNotIn('content_length_bytes', diagnostic)
        self.assertNotIn('timeout_kind', diagnostic)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)

    async def test_real_oversize_content_length_is_recorded_and_rejected_before_body(self):
        await self.proxy([], length=module.MAX_FILE_BYTES + 1)
        aid = self.register(url=URL)
        with self.assertRaises(AttachmentError) as failure:
            await self.store.read(fixtures.MID, aid)
        diagnostic = failure.exception.transport_diagnostic
        self.assertEqual(failure.exception.code, 'attachment_too_large')
        self.assertEqual(diagnostic['content_length_bytes'], module.MAX_FILE_BYTES + 1)
        self.assertNotIn('bytes_received', diagnostic)
        self.assert_clean_failed_read()
        self.assert_safe(diagnostic)


if __name__ == '__main__':
    unittest.main()
