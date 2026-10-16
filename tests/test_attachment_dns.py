"""Independent attachment DNS regressions; mocks or loopback-only synthetic TLS.

No user attachment hostnames, external DNS, external downloads or QQ calls.
"""
import asyncio
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import test_attachments as attachments
import test_callback_dns as dns_fixtures
import test_dns_reuse as reuse_fixtures

module = attachments.module
AttachmentTransport = module.AttachmentTransport
AttachmentError = module.AttachmentError
EventError = module.EventError
ProxySettings = attachments.proxy_module.ProxySettings
HOST = 'media.example.com'
URL = 'https://media.example.com/SECRET_PATH/a%2fb?token=SECRET_QUERY%2F'
PROXY = ProxySettings(True, 'http://127.0.0.1:3128', 'SECRET_USER', 'SECRET_PASSWORD')


def entry(ip='1.1.1.1', **changes):
    return dict(attachments.entry(ip), **changes)


def body(ips=('1.1.1.1',)):
    value = dns_fixtures.answer([dns_fixtures.record(HOST, data=ip) for ip in ips])
    value['Question'] = [{'name': HOST + '.', 'type': 1}]
    return dns_fixtures.encode(value)


class AttachmentDnsTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, *, mode='google', proxy=PROXY):
        transport = AttachmentTransport(proxy=proxy, attachment_dns=mode)
        self.addAsyncCleanup(transport.close)
        return transport

    async def grant(self, transport, url):
        descriptor = {'id': 'att_' + '1' * 32, 'message_id': 'msg_' + '1' * 32,
                      'kind': 'file', 'file_id': 'SECRET_FILE_ID'}
        owner = types.SimpleNamespace(transport=transport, _check=lambda: None,
            _private_http_enabled=lambda: True, descriptor=lambda *_: descriptor)
        scope = module._PrivateHTTPRead(owner, descriptor, conversation_type='private',
            bot=types.SimpleNamespace(call_action=AsyncMock(return_value={'url': url})),
            self_id='123456', guard=lambda: None)
        self.addCleanup(scope.close)
        return await scope.resolve()

    async def test_default_and_explicit_system_do_not_create_google_resolver(self):
        with patch.object(module, 'GoogleResolver') as google:
            default = AttachmentTransport()
            self.addAsyncCleanup(default.close)
            self.assertEqual(default.attachment_dns, 'system')
            self.assertIsNone(default._google_resolver)
            for proxy in (None, PROXY):
                transport = self.transport(mode='system', proxy=proxy)
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                        AsyncMock(return_value=[attachments.dns_entry('1.1.1.1')])) as system:
                    self.assertEqual(await transport._addresses(HOST), [entry()])
                system.assert_awaited_once_with(HOST, 443, family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            google.assert_not_called()

    async def test_bad_modes_and_google_without_enabled_explicit_proxy_fail_closed(self):
        for mode in (None, 'Google', '', False, 1, [], {}, ['google'], 'https://dns.google'):
            with self.subTest(mode=mode), self.assertRaisesRegex(AttachmentError, '^invalid_attachment_dns$'):
                AttachmentTransport(proxy=PROXY, attachment_dns=mode)
        for proxy in (None, ProxySettings(), ProxySettings(False, PROXY.url)):
            with self.subTest(proxy=proxy), self.assertRaisesRegex(AttachmentError, '^attachment_dns_requires_proxy$'):
                AttachmentTransport(proxy=proxy, attachment_dns='google')

    async def test_dns_mode_and_proxy_are_immutable_per_transport(self):
        transport = self.transport()
        with self.assertRaises(AttributeError):
            transport.proxy = ProxySettings()
        with self.assertRaises(AttributeError):
            transport.attachment_dns = 'system'
        self.assertIs(transport._google_resolver.proxy, transport.proxy)

    async def test_system_gaierror_keeps_only_safe_hostname_phase_and_errno(self):
        transport = self.transport(mode='system')
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                AsyncMock(side_effect=socket.gaierror(-2, 'SECRET_EXCEPTION ' + URL))), \
                patch.object(module.aiohttp, 'ClientSession') as session:
            with self.assertRaises(AttachmentError) as result:
                await transport.download(URL, lambda _: None)
        error = result.exception
        self.assertEqual(str(error), 'attachment_dns_failed')
        self.assertEqual(error.dns_diagnostic, {'mode': 'system', 'hostname': HOST,
            'phase': 'system_lookup', 'reason': 'gaierror', 'errno': -2})
        self.assertIsNone(error.source_diagnostic)
        self.assertNotIn('SECRET_', str(error.__dict__))
        session.assert_not_called()

    async def test_system_empty_many_malformed_and_mixed_answers_fail_closed(self):
        transport = self.transport(mode='system')
        cases = [([], 'attachment_dns_failed', 'empty'),
                 ([attachments.dns_entry()] * 17, 'attachment_dns_failed', 'too_many'),
                 (None, 'attachment_dns_failed', 'invalid_response'),
                 ([()], 'attachment_dns_blocked', 'blocked'),
                 ([attachments.dns_entry(), attachments.dns_entry('10.0.0.1')],
                  'attachment_dns_blocked', 'blocked'),
                 ([(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', None)],
                  'attachment_dns_blocked', 'blocked')]
        for answer, code, reason in cases:
            with self.subTest(reason=reason, answer=answer), \
                    patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=answer)):
                with self.assertRaises(AttachmentError) as result:
                    await transport._addresses(HOST)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(result.exception.dns_diagnostic,
                {'mode': 'system', 'hostname': HOST, 'phase': 'validation', 'reason': reason})

    async def test_diagnostic_defensively_sanitizes_fields_and_bounds_errno(self):
        for errno in (True, 'SECRET_ERRNO', 65536, -65536, 10**100, None):
            error = AttachmentError('attachment_dns_failed')
            error.note_dns(mode='system', hostname=URL, phase='SECRET_PHASE',
                           reason='SECRET_REASON', errno=errno)
            self.assertEqual(error.dns_diagnostic, {'mode': 'system', 'hostname': None,
                'phase': 'validation', 'reason': 'invalid_response'})
        error.note_dns(mode='google', hostname=HOST.upper(), phase='queued', reason='timeout', errno=65535)
        self.assertEqual(error.dns_diagnostic['hostname'], HOST)
        self.assertEqual(error.dns_diagnostic['errno'], 65535)
        other = AttachmentError('attachment_dns_failed')
        other.note_dns(mode='SECRET_MODE', hostname=HOST, phase='queued', reason='timeout')
        self.assertIsNone(other.dns_diagnostic)

    async def test_system_oserror_is_fixed_and_does_not_copy_message(self):
        transport = self.transport(mode='system')
        for errno, expected in ((13, 13), (65536, None)):
            with patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                    AsyncMock(side_effect=OSError(errno, 'SECRET_EXCEPTION'))):
                with self.assertRaises(AttachmentError) as result:
                    await transport._addresses(HOST)
            detail = result.exception.dns_diagnostic
            self.assertEqual(detail['reason'], 'oserror')
            self.assertEqual(detail.get('errno'), expected)
            self.assertNotIn('SECRET_', json.dumps(detail))

    async def test_google_discloses_only_validated_hostname_and_pins_download(self):
        transport = self.transport()
        session, context = attachments.fake_session()
        with patch.object(transport._google_resolver, 'resolve', AsyncMock(return_value=[entry()])) as resolve, \
                patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(side_effect=AssertionError('No DNS'))) as system, \
                patch.object(module.aiohttp, 'TCPConnector') as connector, \
                patch.object(module.aiohttp, 'ClientSession', return_value=context):
            await transport.download(URL, lambda _: None)
        resolve.assert_awaited_once()
        self.assertEqual(resolve.await_args.args, (HOST,))
        self.assertEqual(set(resolve.await_args.kwargs), {'timeout', 'detail'})
        self.assertGreater(resolve.await_args.kwargs['timeout'], 0)
        self.assertLessEqual(resolve.await_args.kwargs['timeout'], 5)
        self.assertNotIn('SECRET_', str(resolve.await_args))
        self.assertEqual(str(session.get.call_args.args[0]), 'https://1.1.1.1/SECRET_PATH/a%2fb?token=SECRET_QUERY%2F')
        options = session.get.call_args.kwargs
        self.assertEqual(options['server_hostname'], HOST)
        self.assertEqual(options['headers'], {'Accept-Encoding': 'identity', 'Host': HOST})
        self.assertEqual(options['proxy'], PROXY.url)
        self.assertEqual(options['proxy_auth'], aiohttp.BasicAuth(PROXY.username, PROXY.password))
        self.assertNotIn('ssl', options)
        self.assertFalse(options['allow_redirects'])
        self.assertIsNone(connector.call_args.kwargs['resolver'])
        system.assert_not_called()

    async def test_invalid_or_unsigned_http_sources_never_query_google(self):
        transport = self.transport()
        with patch.object(transport._google_resolver, 'resolve', AsyncMock()) as resolve:
            for source in ('http://' + HOST + '/SECRET', 'https://127.0.0.1/SECRET',
                           'https://8.8.8.8/SECRET', 'https://user:SECRET@' + HOST + '/',
                           'https://' + HOST + ':444/', 'https://bad_host/SECRET',
                           'https://' + HOST + '/#SECRET', {'url': URL}, module._PrivateHTTPGrant()):
                with self.subTest(source=source), self.assertRaises(AttachmentError):
                    await transport.download(source, lambda _: None)
            resolve.assert_not_called()

    async def test_private_http_hostname_grant_uses_google_but_preserves_scheme_port_and_query(self):
        transport = self.transport()
        source = 'http://' + HOST + '/SECRET_PATH/a%2fb?token=SECRET_QUERY'
        grant = await self.grant(transport, source)
        session, context = attachments.fake_session()
        with patch.object(transport._google_resolver, 'resolve', AsyncMock(return_value=[entry()])) as resolve, \
                patch.object(module.aiohttp, 'TCPConnector'), \
                patch.object(module.aiohttp, 'ClientSession', return_value=context):
            await transport.download(grant, lambda _: None)
        self.assertEqual(resolve.await_args.args, (HOST,))
        self.assertEqual(str(session.get.call_args.args[0]), 'http://1.1.1.1/SECRET_PATH/a%2fb?token=SECRET_QUERY')
        self.assertEqual(session.get.call_args.kwargs['headers']['Host'], HOST)
        self.assertNotIn('server_hostname', session.get.call_args.kwargs)
        with self.assertRaisesRegex(AttachmentError, '^attachment_http_grant_invalid$'):
            await transport.download(grant, lambda _: None)

    async def test_authorized_http_literal_never_queries_google_or_system(self):
        transport = self.transport()
        for ip in ('1.1.1.1', '[2606:4700:4700::1111]'):
            source = 'http://' + ip + '/SECRET_PATH?token=SECRET_QUERY'
            grant = await self.grant(transport, source)
            session, context = attachments.fake_session()
            with patch.object(transport._google_resolver, 'resolve', AsyncMock()) as resolve, \
                    patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock()) as system, \
                    patch.object(module.aiohttp, 'TCPConnector'), \
                    patch.object(module.aiohttp, 'ClientSession', return_value=context):
                await transport.download(grant, lambda _: None)
            resolve.assert_not_called()
            system.assert_not_called()
            self.assertEqual(str(session.get.call_args.args[0]), source)

    async def test_google_revalidates_all_returned_addresses_and_count(self):
        transport = self.transport()
        cases = [([], 'attachment_dns_failed', 'empty'),
                 ([entry()] * 17, 'attachment_dns_failed', 'too_many'),
                 ([entry(), entry('10.0.0.1')], 'attachment_dns_blocked', 'blocked'),
                 ([entry(hostname='other.example')], 'attachment_dns_blocked', 'blocked'),
                 ([entry('::ffff:1.1.1.1')], 'attachment_dns_blocked', 'blocked'),
                 ([entry(port=80)], 'attachment_dns_blocked', 'blocked')]
        for answer, code, reason in cases:
            with self.subTest(reason=reason, answer=answer), \
                    patch.object(transport._google_resolver, 'resolve', AsyncMock(return_value=answer)):
                with self.assertRaises(AttachmentError) as result:
                    await transport._addresses(HOST)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(result.exception.dns_diagnostic,
                {'mode': 'google', 'hostname': HOST, 'phase': 'validation', 'reason': reason})

    async def test_google_guard_blocks_before_dns_and_rechecks_queued_read_before_disclosure(self):
        for queued in (False, True):
            transport = self.transport()
            transport._dns_slots = asyncio.Semaphore(0 if queued else 4)
            allowed = queued
            checks = []
            def guard():
                checks.append(allowed)
                if not allowed:
                    raise AttachmentError('attachment_source_unavailable')
            with patch.object(transport._google_resolver, 'resolve', AsyncMock()) as resolve, \
                    patch.object(module.aiohttp, 'ClientSession') as download:
                task = asyncio.create_task(transport.download(URL, lambda _: None, guard=guard))
                if queued:
                    await reuse_fixtures.until(lambda: len(transport._lookups) == 1)
                    self.assertEqual(checks, [True])
                    allowed = False
                    transport._dns_slots.release()
                with self.assertRaisesRegex(AttachmentError, '^attachment_source_unavailable$'):
                    await task
                resolve.assert_not_called()
                download.assert_not_called()
            self.assertEqual(checks, [True, False] if queued else [False])
            self.assertFalse(transport._lookups)

    async def test_google_guard_also_rechecks_after_dns_before_download(self):
        transport = self.transport()
        allowed, checks = True, []
        def guard():
            checks.append(allowed)
            if not allowed:
                raise AttachmentError('attachment_source_unavailable')
        async def resolve(*args, **kwargs):
            nonlocal allowed
            allowed = False
            return [entry()]
        with patch.object(transport._google_resolver, 'resolve', resolve), \
                patch.object(module.aiohttp, 'ClientSession') as download:
            with self.assertRaisesRegex(AttachmentError, '^attachment_source_unavailable$'):
                await transport.download(URL, lambda _: None, guard=guard)
        self.assertEqual(checks, [True, True, False])
        download.assert_not_called()

    async def test_google_failure_maps_codes_and_never_falls_back(self):
        transport = self.transport()
        cases = [('callback_dns_failed', 'attachment_dns_failed', 'invalid_response'),
                 ('callback_dns_blocked', 'attachment_dns_blocked', 'blocked'),
                 ('callback_timeout', 'attachment_timeout', 'timeout'),
                 ('callback_stopping', 'attachment_stopping', 'stopping')]
        for original, expected, reason in cases:
            async def fail(host, *, timeout, detail):
                detail.update(phase='SECRET_PHASE', error='SECRET_REASON', raw='SECRET_DATA')
                raise EventError(original)
            with patch.object(transport._google_resolver, 'resolve', fail), \
                    patch.object(transport, '_system_addresses', AsyncMock(side_effect=AssertionError('No fallback'))) as system:
                with self.assertRaises(AttachmentError) as result:
                    await transport.download(URL, lambda _: None)
            self.assertEqual(result.exception.code, expected)
            self.assertEqual(result.exception.dns_diagnostic,
                {'mode': 'google', 'hostname': HOST, 'phase': 'validation', 'reason': reason})
            self.assertNotIn('SECRET_', str(result.exception.__dict__))
            system.assert_not_called()

    async def test_google_parser_rejects_empty_many_invalid_private_answers_without_download(self):
        transport = self.transport()
        for response_body, code in ((body(()), 'attachment_dns_failed'),
                (body(('1.1.1.1',) * 17), 'attachment_dns_failed'),
                (b'INVALID_SECRET_RESPONSE', 'attachment_dns_failed'),
                (body(('1.1.1.1', '10.0.0.1')), 'attachment_dns_blocked')):
            session, _, _ = dns_fixtures.fake_session(response_body)
            transport._google_resolver._session = session
            with self.subTest(code=code), patch.object(module.aiohttp, 'ClientSession') as download:
                with self.assertRaises(AttachmentError) as result:
                    await transport.download(URL, lambda _: None)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(result.exception.dns_diagnostic['phase'], 'validation')
            self.assertNotIn('SECRET_', str(result.exception.__dict__))
            download.assert_not_called()
            target = session.get.call_args.args[0]
            self.assertEqual(str(target), 'https://8.8.8.8/resolve?name=' + HOST + '&type=A')

    async def test_google_timeout_cancels_inflight_lookup_and_releases_slot(self):
        transport = self.transport()
        cancelled = asyncio.Event()
        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(module, 'DNS_TIMEOUT', .02), \
                patch.object(transport._google_resolver, 'resolve', hang):
            with self.assertRaises(AttachmentError) as result:
                await transport._addresses(HOST)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.exception.code, 'attachment_timeout')
        self.assertEqual(result.exception.dns_diagnostic['reason'], 'timeout')
        self.assertFalse(transport._lookups)
        self.assertEqual(transport._dns_slots._value, 4)

    async def test_google_queue_and_request_share_one_dns_budget(self):
        transport = self.transport()
        transport._dns_slots = asyncio.Semaphore(0)
        async def slow(*args, **kwargs):
            await asyncio.sleep(.04)
            return [entry()]
        loop = asyncio.get_running_loop()
        release = loop.call_later(.03, transport._dns_slots.release)
        self.addCleanup(release.cancel)
        with patch.object(module, 'DNS_TIMEOUT', .05), \
                patch.object(transport._google_resolver, 'resolve', slow):
            with self.assertRaisesRegex(AttachmentError, '^attachment_timeout$'):
                await transport._addresses(HOST)
        self.assertFalse(transport._lookups)

    async def test_system_timeout_retains_worker_slot_until_lookup_really_finishes(self):
        transport = self.transport(mode='system')
        done = asyncio.Event()
        async def hang(*args, **kwargs):
            await done.wait()
            return [attachments.dns_entry()]
        with patch.object(module, 'DNS_TIMEOUT', .02), \
                patch.object(asyncio.get_running_loop(), 'getaddrinfo', hang):
            with self.assertRaises(AttachmentError) as result:
                await transport._addresses(HOST)
            self.assertEqual(result.exception.dns_diagnostic['reason'], 'timeout')
            self.assertEqual(len(transport._lookups), 1)
            self.assertEqual(transport._dns_slots._value, 3)
            done.set()
            await reuse_fixtures.until(lambda: not transport._lookups)
        self.assertEqual(transport._dns_slots._value, 4)

    async def test_close_cancels_active_and_queued_google_jobs_before_pool_close(self):
        transport = self.transport()
        active, cancelled = [], []
        async def hang(host, **kwargs):
            active.append(host)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(host)
        async def close():
            self.assertEqual(len(cancelled), 4)
            self.assertFalse(transport._lookups)
        with patch.object(transport._google_resolver, 'resolve', hang), \
                patch.object(transport._google_resolver, 'close', side_effect=close) as pool_close:
            tasks = [asyncio.create_task(transport._addresses(HOST)) for _ in range(8)]
            await reuse_fixtures.until(lambda: len(active) == 4)
            self.assertEqual(len(transport._lookups), 8)
            await transport.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(transport._dns_slots._value, 4)
        pool_close.assert_awaited_once()
        with self.assertRaisesRegex(AttachmentError, '^attachment_stopping$'):
            await transport._addresses(HOST)

    async def test_caller_cancellation_and_dns_timeout_leave_no_google_jobs(self):
        self.assertEqual((module.DNS_TIMEOUT, module.NETWORK_TIMEOUT), (5, 20))
        transport = self.transport()
        for cancel in (True, False):
            started = asyncio.Event()
            async def hang(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()
            with patch.object(transport._google_resolver, 'resolve', hang), \
                    patch.object(module, 'DNS_TIMEOUT', .03):
                task = asyncio.create_task(transport.download(URL, lambda _: None))
                await started.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaisesRegex(AttachmentError, '^attachment_timeout$'):
                        await task
            self.assertFalse(transport._lookups)
            self.assertEqual(transport._dns_slots._value, 4)


class AttachmentDnsProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_rejected_connect_is_fixed_numeric_proxy_only_and_safe_diagnostic(self):
        proxy = await dns_fixtures.SyntheticDnsProxy().start()
        self.addAsyncCleanup(proxy.close)
        transport = AttachmentTransport(attachment_dns='google',
            proxy=ProxySettings(True, proxy.url, 'fake-user', 'fake-password'))
        self.addAsyncCleanup(transport.close)
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo',
                AsyncMock(side_effect=AssertionError('No external/system DNS'))) as system:
            with self.assertRaises(AttachmentError) as result:
                await transport.download(URL, lambda _: None)
        self.assertEqual(result.exception.dns_diagnostic, {'mode': 'google', 'hostname': HOST,
            'phase': 'connection_or_request', 'reason': 'proxy_failed'})
        self.assertEqual(result.exception.code, 'attachment_dns_failed')
        self.assertEqual(len(proxy.connects), 1)
        self.assertTrue(proxy.connects[0].startswith(b'CONNECT 8.8.8.8:443 HTTP/1.1\r\n'))
        self.assertIn(b'Host: dns.google\r\n', proxy.connects[0])
        self.assertNotIn(HOST.encode(), proxy.connects[0])
        self.assertNotIn(b'SECRET_', proxy.connects[0])
        self.assertFalse(proxy.requests)
        self.assertFalse(proxy.errors)
        system.assert_not_called()


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for loopback TLS')
class AttachmentDnsLoopbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='attachment-dns-tls-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / 'dns.pem'
        cls.key = Path(cls.directory.name) / 'dns.key'
        subprocess.run([shutil.which('openssl'), 'req', '-x509', '-newkey', 'rsa:2048',
            '-nodes', '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
            '-subj', '/CN=dns.google', '-addext', 'subjectAltName=DNS:dns.google'],
            check=True, capture_output=True, timeout=20)

    async def setup_proxy(self, *, trusted=True, **options):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.sni = []
        context.set_servername_callback(lambda _socket, name, _context: self.sni.append(name))
        proxy = await reuse_fixtures.KeepaliveDnsProxy(context, **options).start()
        self.addAsyncCleanup(proxy.close)
        if trusted:
            verified = ssl.create_default_context(cafile=str(self.cert))
            self.assertTrue(verified.check_hostname)
            self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
            self.enterContext(patch.object(aiohttp.connector, '_SSL_CONTEXT_VERIFIED', verified))
        self.enterContext(patch.object(asyncio.get_running_loop(), 'getaddrinfo',
            AsyncMock(side_effect=AssertionError('No external/system DNS'))))
        return proxy, self.transport(proxy)

    def transport(self, proxy, username='fake-user', password='fake-password'):
        transport = AttachmentTransport(attachment_dns='google',
            proxy=ProxySettings(True, proxy.url, username, password))
        self.addAsyncCleanup(transport.close)
        return transport

    async def test_real_tls_pool_reuses_connection_but_revalidates_fresh_answers(self):
        proxy, transport = await self.setup_proxy(bodies=[body(('1.1.1.1',)), body(('8.8.4.4',))])
        with patch.object(dns_fixtures.module, 'parse_google_answers',
                wraps=dns_fixtures.module.parse_google_answers) as validate:
            self.assertEqual(await transport._addresses(HOST), [entry()])
            self.assertEqual(await transport._addresses(HOST), [entry('8.8.4.4')])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (1, 1, 2))
        self.assertEqual(self.sni, ['dns.google'])
        for request in proxy.requests:
            self.assertTrue(request.startswith(b'GET /resolve?name=media.example.com&type=A HTTP/1.1\r\n'))
            self.assertIn(b'Host: dns.google\r\n', request)
            for forbidden in (b'SECRET_', b'Authorization:', b'Cookie:', b'fake-user', b'fake-password'):
                self.assertNotIn(forbidden, request)
        self.assertFalse(proxy.errors)

    async def test_real_tls_pool_close_and_proxy_credentials_reload_are_isolated(self):
        proxy, transport = await self.setup_proxy(bodies=[body(), body()])
        await transport._addresses(HOST)
        session, connector = transport._google_resolver._session, transport._google_resolver._session.connector
        self.assertTrue(connector._conns)
        await transport.close()
        await reuse_fixtures.until(lambda: proxy.closed_count == 1)
        self.assertTrue(session.closed)
        self.assertTrue(connector.closed)
        self.assertFalse(connector._conns)
        self.assertIsNone(connector._cleanup_handle)
        replacement = self.transport(proxy, 'replacement-user', 'replacement-password')
        await replacement._addresses(HOST)
        self.assertIsNot(session, replacement._google_resolver._session)
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 2))
        self.assertNotEqual(proxy.connects[0], proxy.connects[1])

    async def test_real_tls_new_mixed_private_answer_cannot_reuse_old_public_result(self):
        proxy, transport = await self.setup_proxy(bodies=[body(), body(('1.1.1.1', '10.0.0.1'))])
        self.assertEqual(await transport._addresses(HOST), [entry()])
        with self.assertRaises(AttachmentError) as result:
            await transport._addresses(HOST)
        self.assertEqual(result.exception.code, 'attachment_dns_blocked')
        self.assertEqual(result.exception.dns_diagnostic['reason'], 'blocked')
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (1, 1, 2))

    async def test_real_untrusted_tls_never_sends_hostname_query(self):
        proxy, transport = await self.setup_proxy(trusted=False)
        with self.assertRaises(AttachmentError) as result:
            await transport._addresses(HOST)
        self.assertEqual(result.exception.code, 'attachment_dns_failed')
        self.assertEqual(result.exception.dns_diagnostic['reason'], 'tls_failed')
        self.assertEqual(self.sni, ['dns.google'])
        self.assertFalse(proxy.requests)

    async def test_real_cancellation_closes_active_dns_socket_and_never_late_downloads(self):
        proxy, transport = await self.setup_proxy(hold=True)
        task = asyncio.create_task(transport.download(URL, lambda _: self.fail('No download bytes')))
        await reuse_fixtures.until(lambda: len(proxy.requests) == 1)
        await transport.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport._google_resolver._session.closed)
        self.assertFalse(transport._lookups)
        self.assertEqual(transport._dns_slots._value, 4)
        proxy.gate.set()
        await reuse_fixtures.until(lambda: proxy.closed_count == 1)
        self.assertEqual(len(proxy.requests), 1)
        self.assertFalse(proxy.errors)


if __name__ == '__main__':
    unittest.main()
