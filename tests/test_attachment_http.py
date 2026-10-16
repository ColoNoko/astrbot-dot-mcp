"""v047 private-file HTTP compatibility. Synthetic bots, DNS and bytes only.

The real-socket test talks exclusively to a loopback fake forward proxy; it
never resolves or fetches a real QQ attachment and never contacts public IPs.
"""
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from multidict import CIMultiDict

import test_attachment_resolution as resolution
from test_attachment_resolution import HTTP, HTTPS, API_HTTPS, TOKEN
from test_attachments import fake_session, dns_entry, entry


class PrivateHTTPTests(unittest.IsolatedAsyncioTestCase):
    event = resolution.AttachmentResolutionTests.event
    capture = resolution.AttachmentResolutionTests.capture
    read = resolution.AttachmentResolutionTests.read

    async def asyncSetUp(self):
        await resolution.AttachmentResolutionTests.asyncSetUp(self)
        self.config['attachment_private_http'] = True
        self.urls, self.grants, self.scopes = [], [], []
        original_download = self.transport.download
        original_validate = self.transport.validate
        def validate(source):
            if type(source) is self.errors._PrivateHTTPGrant:
                return self.errors._http_url(self.errors._http_grant(source, transport=self.transport)['url'])
            return original_validate(source)
        self.transport.validate = validate
        async def download(source, consume, *, guard=None):
            if type(source) is self.errors._PrivateHTTPGrant:
                self.grants.append(source)
                self.urls.append(self.errors._http_grant(source)['url'])
            else:
                self.urls.append(source)
            await original_download(source, consume, guard=guard)
        self.transport.download = download

    async def asyncTearDown(self):
        for scope in self.scopes:
            scope.close()
        await resolution.AttachmentResolutionTests.asyncTearDown(self)
        self.assertEqual(len(self.errors._HTTP_GRANTS), 0)

    def private_file(self, url=HTTP, *, raw_url=HTTP, file_id='captured-file-id'):
        event = self.event({'file_id': file_id, 'url': raw_url, 'name': '../../untrusted.exe'}, kind='file')
        event.bot.call_action.return_value = {'url': url}
        return event

    def metadata(self, result):
        return json.loads(result['content'][1]['text'])

    async def use_real_transport(self, proxy=None):
        transport = self.errors.AttachmentTransport(proxy=proxy)
        self.cache.transport = transport
        return transport

    async def grant(self, url=HTTP, transport=None):
        event = self.private_file(url)
        mid, aid = await self.capture(event)
        descriptor = self.cache.descriptor(mid, aid)
        scope = self.cache.private_http_read(descriptor, conversation_type='private',
                    bot=event.bot, self_id=event.message_obj.raw_message['self_id'], guard=lambda: None)
        self.scopes.append(scope)
        self.addCleanup(scope.close)
        return await scope.resolve(), scope, event, mid, aid

    async def test_private_api_http_requires_exact_true_and_fresh_exact_action(self):
        event = self.private_file()
        result = await self.read(event)
        event.bot.call_action.assert_awaited_once_with(action='get_private_file_url',
                                              file_id='captured-file-id', self_id=100)
        self.assertEqual(self.urls, [HTTP])
        self.assertEqual(result['structuredContent']['transport'], 'http')
        self.assertEqual(result['structuredContent']['integrity'], 'unverified')
        self.assertEqual(json.loads(result['content'][0]['text'])['transport'], 'http')
        meta = self.metadata(result)
        self.assertEqual(meta['transport'], 'http')
        self.assertEqual(meta['integrity'], 'unverified')
        self.assertEqual(meta['sha256_scope'], 'local_cache_consistency_only')
        self.assertTrue(meta['untrusted'])
        self.assertEqual(meta['filename'], 'untrusted.exe')
        self.assertNotIn('secret=', json.dumps(result))
        self.assertNotIn('captured-file-id', json.dumps(result))
        self.assertEqual(len(self.errors._HTTP_GRANTS), 0)
        self.assertTrue(self.grants)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            self.transport.validate(self.grants[0])

    async def test_false_absent_and_truthy_nonbooleans_never_enable_http(self):
        for mode in (False, None, 1, 'true', 'false', [], {'enabled': True}):
            with self.subTest(mode=mode):
                if mode is None:
                    self.config.pop('attachment_private_http', None)
                else:
                    self.config['attachment_private_http'] = mode
                with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_https_required'):
                    await self.read(self.private_file())
        self.assertEqual(self.urls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_http_not_authorized_for_images_groups_missing_file_id_or_raw_only(self):
        # The private compatibility switch alone never grants group HTTP.
        self.config['group_file_cache_enabled'] = False
        cases = [self.event({'file': TOKEN, 'url': HTTP}),
                 self.event({'file_id': 'captured-file-id', 'url': HTTP}, kind='file', group=True),
                 self.private_file(file_id=None)]
        no_api = self.private_file()
        no_api.bot.call_action = None
        cases.append(no_api)
        for event in cases:
            if callable(getattr(event.bot, 'call_action', None)):
                event.bot.call_action.return_value = {'url': HTTP}
            with self.subTest(kind=event.messages[-1].__class__.__name__):
                with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_https_required'):
                    await self.read(event)
        self.assertEqual(self.urls, [])

    async def test_opaque_grant_cannot_be_forged_with_strings_dict_flags_or_new_instance(self):
        transport = self.errors.AttachmentTransport()
        self.addAsyncCleanup(transport.close)
        for source in (HTTP, {'url': HTTP, 'allow_http': True}, (HTTP, True),
                       self.errors._PrivateHTTPGrant()):
            with self.subTest(source=type(source).__name__), self.assertRaises(self.errors.AttachmentError):
                transport.validate(source)
        with self.assertRaises(TypeError):
            await transport.download(HTTP, lambda _: None, allow_http=True)

    async def test_corrupt_registered_provenance_never_calls_private_api(self):
        for field, value in [('segment_index', 0), ('kind', 'image'),
                             ('source_file_id', 'different-file-id'), ('source_file_id', ''),
                             ('source_url', 'http://other.example.com/secret')]:
            event = self.private_file()
            mid, aid = await self.capture(event)
            with self.cache.db:
                self.cache.db.execute('UPDATE message_attachments SET '+field+'=? WHERE id=?', (value, aid))
            with self.subTest(field=field), self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_source_unavailable'):
                await self.plugin._read_attachment(mid, aid)
            event.bot.call_action.assert_not_called()
        self.assertEqual(self.urls, [])

    async def test_wrong_message_attachment_id_never_calls_api(self):
        first, second = self.private_file(), self.private_file()
        _, aid = await self.capture(first)
        mid, _ = await self.capture(second)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_not_found'):
            await self.plugin._read_attachment(mid, aid)
        first.bot.call_action.assert_not_called()
        second.bot.call_action.assert_not_called()

    async def test_cache_hit_requires_new_private_api_and_live_capture_every_time(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        await self.plugin._read_attachment(mid, aid)
        result = await self.plugin._read_attachment(mid, aid)
        self.assertEqual(event.bot.call_action.await_count, 2)
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(self.metadata(result)['transport'], 'http')
        listing = self.cache.list(mid)[0]
        self.assertEqual((listing['transport'], listing['integrity']), ('http', 'unverified'))
        self.config['attachment_private_http'] = False
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_https_required'):
            await self.plugin._read_attachment(mid, aid)
        self.assertEqual(len(self.urls), 1)
        self.config['attachment_private_http'] = True
        event.bot.call_action.side_effect = RuntimeError('private API secret')
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_api_failed'):
            await self.plugin._read_attachment(mid, aid)
        self.assertEqual(len(self.urls), 1)

    async def test_cached_http_descriptor_tampering_does_not_bypass_provenance(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.reset_mock()
        with self.cache.db:
            self.cache.db.execute('UPDATE message_attachments SET source_file_id=? WHERE id=?', ('other', aid))
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_source_unavailable'):
            await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.assert_not_called()
        self.assertEqual(len(self.urls), 1)

    async def test_cached_http_can_only_become_https_after_fresh_https_download(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        await self.plugin._read_attachment(mid, aid)
        self.config['attachment_private_http'] = False
        event.bot.call_action.return_value = {'url': API_HTTPS}
        result = await self.plugin._read_attachment(mid, aid)
        self.assertEqual(self.urls, [HTTP, API_HTTPS])
        self.assertEqual(self.metadata(result)['transport'], 'https')
        self.assertEqual(self.metadata(result)['integrity'], 'unverified')

    async def test_cache_reload_keeps_provenance_but_requires_new_capture_and_mode(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        await self.plugin._read_attachment(mid, aid)
        await self.plugin.terminate()
        self.config['attachment_private_http'] = False
        await self.plugin.initialize()
        self.cache = self.plugin.host.store.attachments
        with self.assertRaisesRegex(self.module.Rejected, 'reactive_context_unavailable'):
            await self.plugin._read_attachment(mid, aid)
        self.assertEqual(self.cache.list(mid)[0]['transport'], 'http')
        self.plugin.media_types = {'image': resolution.Image, 'file': resolution.File}
        await self.plugin._on_message(event)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_https_required'):
            await self.plugin._read_attachment(mid, aid)

    async def test_disable_or_identity_change_during_api_blocks_all_fallbacks(self):
        for change in ('mode', 'enabled', 'config', 'bot', 'host', 'capture', 'self_id', 'file_id'):
            event = self.private_file(raw_url=HTTPS)
            mid, aid = await self.capture(event)
            entered, release = asyncio.Event(), asyncio.Event()
            original_host, original_bot, original_config = self.plugin.host, event.bot, self.plugin.config
            async def action(**kwargs):
                entered.set()
                await release.wait()
                return {'url': HTTP}
            event.bot.call_action.side_effect = action
            task = asyncio.create_task(self.plugin._read_attachment(mid, aid))
            await asyncio.wait_for(entered.wait(), 2)
            if change == 'mode': self.config['attachment_private_http'] = False
            elif change == 'enabled': self.config['enabled'] = False
            elif change == 'config': self.plugin.config = dict(self.config)
            elif change == 'bot': event.bot = types.SimpleNamespace(call_action=AsyncMock())
            elif change == 'host': self.plugin.host = types.SimpleNamespace()
            elif change == 'capture': self.plugin.captured.pop(mid)
            elif change == 'self_id': event.message_obj.raw_message['self_id'] = 999
            elif change == 'file_id': event.message_obj.raw_message['message'][1]['data']['file_id'] = 'changed'
            release.set()
            with self.subTest(change=change), self.assertRaises(Exception):
                await task
            self.assertEqual(self.urls, [])
            self.assertEqual(list(self.directory.iterdir()), [])
            self.plugin.host, event.bot, self.plugin.config = original_host, original_bot, original_config
            self.config.update(enabled=True, attachment_private_http=True)

    async def test_disable_during_stream_removes_partial_and_does_not_cache(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(source, consume, *, guard=None):
            consume(b'first')
            entered.set()
            await release.wait()
            consume(b'second')
        self.transport.download = stream
        task = asyncio.create_task(self.plugin._read_attachment(mid, aid))
        await asyncio.wait_for(entered.wait(), 2)
        self.config['attachment_private_http'] = False
        release.set()
        with self.assertRaises(Exception):
            await task
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(self.cache.list(mid)[0]['size'])
        self.assertEqual(len(self.errors._HTTP_GRANTS), 0)

    async def test_disable_after_last_chunk_before_download_returns_does_not_cache(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        async def stream(source, consume, *, guard=None):
            consume(b'complete')
            self.config['attachment_private_http'] = False
        self.transport.download = stream
        with self.assertRaises(Exception):
            await self.plugin._read_attachment(mid, aid)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(self.cache.list(mid)[0]['size'])

    async def test_cancellation_revokes_grant_and_cleans_staging_file(self):
        event = self.private_file()
        mid, aid = await self.capture(event)
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.plugin._read_attachment(mid, aid))
        await asyncio.wait_for(self.transport.started.wait(), 2)
        self.assertEqual(len(self.errors._HTTP_GRANTS), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.errors._HTTP_GRANTS), 0)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_grants_expire_revoke_cannot_repeat_api_or_cross_descriptor_transport(self):
        transport = await self.use_real_transport()
        grant, scope, event, mid, aid = await self.grant()
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            await scope.resolve()
        event.bot.call_action.assert_awaited_once()
        wrong = dict(scope.descriptor, id='att_'+'f'*32)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            self.errors._http_grant(grant, owner=self.cache, descriptor=wrong)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            self.errors._http_grant(grant, owner=object())
        other = self.errors.AttachmentTransport()
        self.addAsyncCleanup(other.close)
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            other.validate(grant)
        self.errors._HTTP_GRANTS[grant]['expires'] = 0
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_expired'):
            transport.validate(grant)
        scope.close()
        with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
            transport.validate(grant)

    async def test_live_grant_cannot_be_downloaded_twice(self):
        transport = await self.use_real_transport()
        grant, scope, event, mid, aid = await self.grant()
        session, context = fake_session()
        with patch.object(transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
             patch.object(self.errors.aiohttp, 'TCPConnector'), \
             patch.object(self.errors.aiohttp, 'ClientSession', return_value=context):
            await transport.download(grant, lambda _: None)
            with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_http_grant_invalid'):
                await transport.download(grant, lambda _: None)
        self.assertEqual(session.get.call_count, 1)

    async def test_http_url_shape_private_literals_odd_ports_credentials_fragments_blocked(self):
        urls = ['http://127.0.0.1/file', 'http://10.0.0.1/file', 'http://169.254.169.254/file',
                'http://[::1]/file', 'http://[::ffff:8.8.8.8]/file', 'http://[2002:0808:0808::]/file',
                'http://media.example.com:81/file', 'http://media.example.com:443/file',
                'http://user:secret@media.example.com/file', 'http://media.example.com/file#',
                'http://media.example.com/file#token', 'http://media.example.com./file',
                'http://media%2eexample.com/file', 'http://media.example.com\\@evil.test/file',
                'http://media.example.com/file\n', 'http://2130706433/file', 'http://127.1/file',
                'http://0177.0.0.1/file', 'http://[bad/file', 'file:///tmp/secret',
                'base64://secret', 'data:secret', '/tmp/secret']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(self.errors.AttachmentError):
                await self.read(self.private_file(url))
        self.assertEqual(self.urls, [])

    async def test_dns_all_answers_must_be_public_at_port_80(self):
        transport = await self.use_real_transport()
        loop = asyncio.get_running_loop()
        for ips in (['127.0.0.1'], ['8.8.8.8', '10.0.0.2'], ['169.254.169.254'],
                    ['::1'], ['::ffff:8.8.8.8'], ['100.64.0.1'], ['2002:0808:0808::'], ['192.0.2.1']):
            with self.subTest(ips=ips), patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[dns_entry(ip, 80) for ip in ips])):
                with self.assertRaisesRegex(self.errors.AttachmentError, 'attachment_dns_blocked'):
                    await transport._addresses('media.example.com', 80)
        for records in ([], [dns_entry('8.8.8.8', 443)], [dns_entry('8.8.8.8', 80)]*17):
            with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=records)), self.assertRaises(self.errors.AttachmentError):
                await transport._addresses('media.example.com', 80)
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[dns_entry('8.8.8.8', 80)])) as lookup:
            values = await transport._addresses('media.example.com', 80)
        self.assertEqual(lookup.call_args.args, ('media.example.com', 80))
        resolver = self.errors._PinnedResolver('media.example.com', values, 80)
        self.assertEqual(await resolver.resolve('media.example.com', 80), values)
        for host, port in [('other.example.com', 80), ('media.example.com', 443)]:
            with self.assertRaises(self.errors.AttachmentError):
                await resolver.resolve(host, port)

    async def test_canonical_public_ip_literals_do_not_trigger_dns(self):
        transport = await self.use_real_transport()
        loop = asyncio.get_running_loop()
        for ip in ('8.8.8.8', '2001:4860:4860::8888'):
            url = 'http://' + ('['+ip+']' if ':' in ip else ip) + '/a%2Fb?x=1%2F2'
            grant, scope, _, _, _ = await self.grant(url)
            with patch.object(loop, 'getaddrinfo', AsyncMock()) as lookup:
                addresses = await transport._addresses(ip, 80)
            lookup.assert_not_called()
            self.assertEqual(addresses[0]['host'], ip)
            self.assertEqual(transport.validate(grant).scheme, 'http')
            scope.close()

    async def test_direct_http_preserves_url_pins_dns_no_cookies_auth_or_ambient_proxy(self):
        transport = await self.use_real_transport()
        url = 'http://media.example.com:80/a%2fb?secret=x%2FY&empty='
        grant, scope, _, _, _ = await self.grant(url)
        session, context = fake_session()
        chunks = []
        with patch.object(transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
             patch.object(self.errors.aiohttp, 'TCPConnector') as connector, \
             patch.object(self.errors.aiohttp, 'ClientSession', return_value=context) as client:
            await transport.download(grant, chunks.append)
        request_url = session.get.call_args.args[0]
        self.assertEqual(request_url.raw_authority, 'media.example.com:80')
        self.assertEqual(request_url.raw_path_qs, '/a%2fb?secret=x%2FY&empty=')
        self.assertEqual(request_url.port, 80)
        options = session.get.call_args.kwargs
        self.assertEqual(options, {'headers': {'Accept-Encoding': 'identity'}, 'allow_redirects': False})
        self.assertEqual(connector.call_args.kwargs['resolver'].port, 80)
        self.assertFalse(connector.call_args.kwargs['use_dns_cache'])
        self.assertFalse(client.call_args.kwargs['trust_env'])
        self.assertFalse(client.call_args.kwargs['auto_decompress'])
        self.assertIsInstance(client.call_args.kwargs['cookie_jar'], aiohttp.DummyCookieJar)
        self.assertEqual(chunks, [b'bytes'])

    async def test_http_proxy_pins_numeric_authority_and_never_falls_back(self):
        proxy_module = importlib.import_module('plugin_mock.proxy_config')
        proxy = proxy_module.ProxySettings(True, 'http://127.0.0.1:3128', 'proxyuser', 'proxypass')
        transport = await self.use_real_transport(proxy)
        for error in (None, aiohttp.ClientConnectionError('secret proxy URL')):
            grant, scope, _, _, _ = await self.grant('http://media.example.com/a%2fb?x=x%2FY')
            session, context = fake_session(error=error)
            with patch.object(transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
                 patch.object(self.errors.aiohttp, 'TCPConnector') as connector, \
                 patch.object(self.errors.aiohttp, 'ClientSession', return_value=context):
                if error:
                    with self.assertRaisesRegex(self.errors.AttachmentError, '^attachment_network_failed$'):
                        await transport.download(grant, lambda _: None)
                else:
                    await transport.download(grant, lambda _: None)
            options = session.get.call_args.kwargs
            self.assertEqual(str(session.get.call_args.args[0]), 'http://8.8.8.8/a%2fb?x=x%2FY')
            self.assertEqual(options['headers'], {'Accept-Encoding': 'identity', 'Host': 'media.example.com'})
            self.assertNotIn('server_hostname', options)
            self.assertEqual(options['proxy'], proxy.url)
            self.assertEqual(options['proxy_auth'], aiohttp.BasicAuth('proxyuser', 'proxypass'))
            self.assertEqual(session.get.call_count, 1)
            scope.close()

    async def test_disable_during_dns_prevents_http_session(self):
        transport = await self.use_real_transport()
        event = self.private_file()
        mid, aid = await self.capture(event)
        entered, release = asyncio.Event(), asyncio.Event()
        async def addresses(host, port):
            entered.set()
            await release.wait()
            return [dict(entry(), port=80)]
        with patch.object(transport, '_addresses', addresses), patch.object(self.errors.aiohttp, 'ClientSession') as client:
            task = asyncio.create_task(self.plugin._read_attachment(mid, aid))
            await asyncio.wait_for(entered.wait(), 2)
            self.config['attachment_private_http'] = False
            release.set()
            with self.assertRaises(Exception):
                await task
        client.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_response_limits_redirect_encoding_and_empty_are_fail_closed(self):
        transport = await self.use_real_transport()
        cases = [({'status': 302}, 'attachment_redirect_blocked'),
                 ({'status': 404}, 'attachment_http_failed'),
                 ({'headers': {'Content-Encoding': 'gzip'}}, 'attachment_encoding_unsupported'),
                 ({'headers': CIMultiDict([('Content-Encoding', 'identity'), ('Content-Encoding', 'gzip')])}, 'attachment_encoding_unsupported'),
                 ({'headers': {'Content-Encoding': 'identity, gzip'}}, 'attachment_encoding_unsupported'),
                 ({'chunks': ()}, 'attachment_empty'),
                 ({'length': self.errors.MAX_FILE_BYTES+1}, 'attachment_too_large'),
                 ({'chunks': (b'x'*(self.errors.MAX_FILE_BYTES+1),)}, 'attachment_too_large')]
        for args, code in cases:
            grant, scope, _, _, _ = await self.grant()
            session, context = fake_session(**args)
            with self.subTest(code=code), patch.object(transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
                 patch.object(self.errors.aiohttp, 'TCPConnector'), \
                 patch.object(self.errors.aiohttp, 'ClientSession', return_value=context):
                with self.assertRaisesRegex(self.errors.AttachmentError, '^'+code+'$'):
                    await transport.download(grant, lambda _: None)
            self.assertEqual(session.get.call_count, 1)
            scope.close()

    async def test_dns_timeout_is_bounded_and_releases_grants(self):
        transport = await self.use_real_transport()
        entered = asyncio.Event()
        async def addresses(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', addresses), \
                patch.object(self.errors, 'DNS_TIMEOUT', .03):
            with self.assertRaisesRegex(self.errors.AttachmentError, '^attachment_timeout$'):
                await self.read(self.private_file())
        self.assertTrue(entered.is_set())
        self.assertEqual(len(self.errors._HTTP_GRANTS), 0)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_real_http_wire_through_synthetic_loopback_proxy_only(self):
        requests, writers = [], []
        async def handler(reader, writer):
            writers.append(writer)
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                requests.append(request.decode('ascii'))
                body = b'only synthetic test bytes'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+
                             b'\r\nConnection: close\r\nSet-Cookie: ignored=secret\r\n\r\n'+body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        proxy_module = importlib.import_module('plugin_mock.proxy_config')
        proxy = proxy_module.ProxySettings(True, 'http://127.0.0.1:'+str(port), 'synthetic', 'test-only')
        transport = await self.use_real_transport(proxy)
        self.config.update(proxy_enabled=True, proxy_url=proxy.url,
                           proxy_username=proxy.username, proxy_password=proxy.password)
        url = 'http://media.example.com/a%2fb?token=synthetic%2Fonly'
        event = self.private_file(url)
        with patch.object(transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])):
            result = await self.read(event)
        self.assertFalse(result['isError'])
        resource = result['content'][2]['resource']
        self.assertEqual(base64.b64decode(resource['blob']), b'only synthetic test bytes')
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertTrue(request.startswith('GET http://8.8.8.8/a%2fb?token=synthetic%2Fonly HTTP/1.1\r\n'))
        self.assertIn('\r\nHost: media.example.com\r\n', request)
        self.assertIn('\r\nProxy-Authorization: Basic ', request)
        self.assertNotIn('\r\nAuthorization:', request)
        self.assertNotIn('\r\nCookie:', request)
        self.assertEqual(result['structuredContent']['integrity'], 'unverified')


class AttachmentHTTPMigrationTests(unittest.IsolatedAsyncioTestCase):
    def old_database(self):
        self.module = importlib.import_module('astrbot_dot_plugin.attachments')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE messages(id TEXT PRIMARY KEY)')
        self.mid, self.aid = 'msg_'+'1'*32, 'att_'+'2'*32
        self.db.execute('INSERT INTO messages VALUES(?)', (self.mid,))
        self.db.execute('''CREATE TABLE message_attachments(
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
            kind TEXT NOT NULL, filename TEXT NOT NULL, source_url TEXT NOT NULL,
            source_file_id TEXT NOT NULL, size INTEGER,sha256 TEXT,mime_type TEXT,
            cached_at INTEGER, accessed_at INTEGER,state TEXT NOT NULL DEFAULT 'registered')''')
        self.db.execute('''INSERT INTO message_attachments(id,message_id,segment_index,kind,filename,
            source_url,source_file_id,state) VALUES(?,?,0,'file','old.bin',?,'','fetching')''',
            (self.aid, self.mid, HTTPS))
        self.db.commit()
        self.directory = Path(self.temp.name)/'cache'

    async def test_old_schema_migrates_default_https_without_claiming_authenticity(self):
        self.old_database()
        store = self.module.Attachments(self.db, self.directory)
        self.addAsyncCleanup(store.close)
        row = store._row(self.mid, self.aid)
        self.assertEqual(row['source_transport'], 'https')
        self.assertEqual(row['source_integrity'], 'unverified')
        self.assertEqual(row['state'], 'registered')
        self.assertEqual(row['source_url'], HTTPS)
        self.assertFalse(self.db.in_transaction)
        await store.close()
        second = self.module.Attachments(self.db, self.directory)
        self.addAsyncCleanup(second.close)
        self.assertEqual(second._row(self.mid, self.aid)['source_transport'], 'https')

    async def test_partial_schema_migration_failure_rolls_back_both_new_columns(self):
        self.old_database()
        def authorize(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_ALTER_TABLE:
                # Both ALTERs use identical authorizer arguments, count them.
                authorize.count += 1
                if authorize.count == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        authorize.count = 0
        self.db.set_authorizer(authorize)
        with self.assertRaises(sqlite3.DatabaseError):
            self.module.Attachments(self.db, self.directory)
        self.db.set_authorizer(None)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(message_attachments)')}
        self.assertNotIn('source_transport', columns)
        self.assertNotIn('source_integrity', columns)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT state FROM message_attachments').fetchone()[0], 'fetching')
        store = self.module.Attachments(self.db, self.directory)
        self.addAsyncCleanup(store.close)
        self.assertEqual(store._row(self.mid, self.aid)['source_transport'], 'https')
