"""Attachment regressions. Fake transports/DNS only; no external downloads."""
import asyncio
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
module = importlib.import_module('astrbot_dot_plugin.attachments')
proxy_module = importlib.import_module('astrbot_dot_plugin.proxy_config')
Attachments, AttachmentTransport, AttachmentError = module.Attachments, module.AttachmentTransport, module.AttachmentError
MID = 'msg_' + '1' * 32
OTHER = 'msg_' + '2' * 32
URL = 'https://media.example.com/a%2fb?secret=private%2Ftoken'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=')


def entry(ip='8.8.8.8'):
    return {'hostname': 'media.example.com', 'host': ip, 'port': 443,
            'family': socket.AF_INET6 if ':' in ip else socket.AF_INET,
            'proto': socket.IPPROTO_TCP, 'flags': socket.AI_NUMERICHOST}


def dns_entry(ip='8.8.8.8', port=443):
    return (socket.AF_INET6 if ':' in ip else socket.AF_INET,
            socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))


def fake_session(status=200, chunks=(b'bytes',), length=None, headers=None, error=None):
    class Content:
        async def iter_chunked(self, size):
            for chunk in chunks:
                yield chunk
    response = types.SimpleNamespace(status=status, content_length=length,
                                     headers=headers or {}, content=Content())
    response_context = AsyncMock()
    response_context.__aenter__.return_value = response
    response_context.__aenter__.side_effect = error
    session = types.SimpleNamespace(get=Mock(return_value=response_context))
    context = AsyncMock()
    context.__aenter__.return_value = session
    return session, context


class FakeTransport:
    def __init__(self, body=PNG):
        self.body, self.calls, self.block = body, [], None
        self.started = asyncio.Event()
        self.error = None

    def validate(self, value):
        return AttachmentTransport().validate(value)

    async def download(self, url, consume, *, guard=None):
        self.calls.append(url)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        for offset in range(0, len(self.body), 65536):
            consume(self.body[offset:offset+65536])
        if self.error:
            raise self.error

    async def close(self):
        pass


class AttachmentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE messages(id TEXT PRIMARY KEY)')
        self.db.executemany('INSERT INTO messages VALUES(?)', [(MID,), (OTHER,)])
        self.db.commit()
        self.addCleanup(self.db.close)
        self.transport = FakeTransport()
        self.directory = Path(self.temp.name) / 'cache'
        self.store = Attachments(self.db, self.directory, transport=self.transport)
        self.addAsyncCleanup(self.store.close)

    def register(self, kind='image', url=URL, file_id='', filename='photo.png', index=0, mid=MID):
        with self.db:
            self.db.execute('BEGIN')
            result = self.store.register(mid, [{'kind': kind, 'segment_index': index,
                          'filename': filename, 'url': url, 'file_id': file_id}])
        return result[0]['attachment_id']

    async def test_lazy_registration_opaque_id_safe_metadata_and_binding(self):
        aid = self.register(filename='../../evil\u202e.png')
        self.assertRegex(aid, r'^att_[a-f0-9]{32}$')
        self.assertEqual(self.transport.calls, [])
        result = self.store.list(MID)
        self.assertEqual(set(result[0]), {'attachment_id', 'kind', 'name', 'mime_type', 'size', 'state'})
        self.assertEqual(result[0]['state'], 'unfetched')
        self.assertEqual(result[0]['name'], 'evil_.png')
        self.assertNotIn('secret', json.dumps(result))
        with self.assertRaisesRegex(AttachmentError, '^attachment_not_found$'):
            await self.store.read(OTHER, aid)
        with self.assertRaisesRegex(AttachmentError, '^attachment_not_found$'):
            await self.store.read(MID, '../../etc/passwd')
        self.assertEqual(self.transport.calls, [])

    async def test_image_roundtrip_hash_mime_atomic_private_cache_and_cache_hit(self):
        aid = self.register(filename='fake.exe')
        result = await self.store.read(MID, aid)
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][1]['type'], 'image')
        self.assertEqual(result['content'][1]['mimeType'], 'image/png')
        self.assertEqual(base64.b64decode(result['content'][1]['data']), PNG)
        metadata = json.loads(result['content'][0]['text'])
        self.assertEqual(metadata['sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(metadata['size_bytes'], len(PNG))
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        cached = self.directory / (aid + '.bin')
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o600)
        self.assertEqual(cached.stat().st_nlink, 1)
        self.assertEqual(list(self.directory.iterdir()), [cached])
        await self.store.read(MID, aid)
        self.assertEqual(self.transport.calls, [URL])

    async def test_file_resource_blob_ignores_filename_and_http_type(self):
        self.transport.body = b'#!/bin/sh\necho never-executed\n'
        aid = self.register(kind='file', filename='script.png')
        result = await self.store.read(MID, aid)
        resource = result['content'][1]['resource']
        self.assertEqual(resource['mimeType'], 'application/octet-stream')
        self.assertEqual(base64.b64decode(resource['blob']), self.transport.body)
        self.assertEqual(resource['uri'], 'qq-attachment://' + MID + '/' + aid)

    async def test_unrecognized_image_downgraded_to_opaque_file(self):
        self.transport.body = b'<svg onload="run()"></svg>'
        aid = self.register(kind='image', filename='picture.svg')
        result = await self.store.read(MID, aid)
        self.assertEqual(result['content'][1]['type'], 'resource')
        self.assertEqual(result['content'][1]['resource']['mimeType'], 'application/octet-stream')

    async def test_exact_private_resolver_only_gets_registered_segment(self):
        aid = self.register(kind='file', url='', file_id='captured-file-id', index=7)
        resolver = AsyncMock(return_value=URL)
        await self.store.read(MID, aid, resolver)
        descriptor = resolver.call_args.args[0]
        self.assertEqual(descriptor['file_id'], 'captured-file-id')
        self.assertEqual(descriptor['segment_index'], 7)
        self.assertEqual(descriptor['message_id'], MID)
        self.assertEqual(self.transport.calls, [URL])

    async def test_missing_resolver_and_unsafe_resolved_url_fail_closed(self):
        aid = self.register(kind='file', url='', file_id='captured-file-id')
        with self.assertRaisesRegex(AttachmentError, '^attachment_source_unavailable$'):
            await self.store.read(MID, aid)
        for url in ('file:///etc/passwd', 'data:text/plain,hello', 'http://media.example.com/file'):
            with self.assertRaises(AttachmentError):
                await self.store.read(MID, aid, AsyncMock(return_value=url))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_unavailable_media_stays_registered_without_dropping_message(self):
        aid = self.register(url=None, file_id=None)
        self.assertEqual(self.store.list(MID)[0]['state'], 'unavailable')
        with self.assertRaisesRegex(AttachmentError, '^attachment_source_unavailable$'):
            await self.store.read(MID, aid)
        self.assertEqual(self.transport.calls, [])

    async def test_resolver_runs_for_captured_url_after_waiting_for_read_lock(self):
        aid = self.register()
        await self.store._lock.acquire()
        resolver = AsyncMock(return_value=URL)
        task = asyncio.create_task(self.store.read(MID, aid, resolver))
        await asyncio.sleep(0)
        resolver.assert_not_awaited()
        self.store._lock.release()
        await task
        resolver.assert_awaited_once()

    async def test_constructor_never_commits_an_unrelated_transaction(self):
        self.db.execute('BEGIN')
        self.db.execute('DELETE FROM messages WHERE id=?', (OTHER,))
        with self.assertRaisesRegex(AttachmentError, '^attachment_transaction_busy$'):
            Attachments(self.db, self.directory, transport=FakeTransport())
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages WHERE id=?', (OTHER,)).fetchone()[0], 1)

    async def test_registration_joins_transaction_and_rolls_back(self):
        with self.assertRaisesRegex(AttachmentError, '^attachment_transaction_required$'):
            self.store.register(MID, [])
        self.db.execute('BEGIN')
        self.store.register(MID, [{'kind': 'file', 'segment_index': 0, 'url': URL}])
        self.db.rollback()
        self.assertEqual(self.store.list(MID), [])
        self.register()
        with self.db:
            self.db.execute('DELETE FROM messages WHERE id=?', (MID,))
        self.assertEqual(self.store.list(MID), [])

    async def test_duplicate_bad_indices_and_missing_source_rejected_atomically(self):
        for specs in ([{'kind': 'image', 'segment_index': True, 'url': URL}],
                      [{'kind': 'image', 'segment_index': 0, 'file_id': 42}],
                      [{'kind': 'image', 'segment_index': 0, 'url': URL}] * 2,
                      [{'kind': 'image', 'segment_index': 256, 'url': URL}]):
            self.db.execute('BEGIN')
            with self.assertRaisesRegex(AttachmentError, '^attachment_descriptor_invalid$'):
                self.store.register(MID, specs)
            self.db.rollback()
            self.assertEqual(self.store.list(MID), [])

    async def test_encoded_output_cap_is_honest_and_never_returns_partial_bytes(self):
        self.transport.body = b'x' * (3 * 1024 * 1024)
        aid = self.register(kind='file')
        with patch.object(module, 'MAX_OUTPUT_BYTES', 4 * 1024 * 1024):
            result = await self.store.read(MID, aid)
        self.assertTrue(result['isError'])
        self.assertEqual(len(result['content']), 1)
        metadata = json.loads(result['content'][0]['text'])
        self.assertEqual(metadata['error'], 'attachment_output_too_large')
        self.assertFalse(metadata['available_in_response'])
        self.assertEqual(metadata['size_bytes'], len(self.transport.body))
        self.assertEqual(metadata['sha256'], hashlib.sha256(self.transport.body).hexdigest())
        self.assertLess(len(json.dumps(result).encode()), module.MAX_OUTPUT_BYTES)

    async def test_stream_byte_limit_removes_partial_file(self):
        self.transport.body = b'x' * 1025
        aid = self.register(kind='file')
        with patch.object(module, 'MAX_FILE_BYTES', 1024):
            with self.assertRaisesRegex(AttachmentError, '^attachment_too_large$'):
                await self.store.read(MID, aid)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(self.store.list(MID)[0]['size'])

    async def test_download_failure_and_cancellation_remove_partial_files(self):
        aid = self.register()
        self.transport.error = AttachmentError('attachment_network_failed')
        with self.assertRaises(AttachmentError):
            await self.store.read(MID, aid)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.transport.error = None
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.store.read(MID, aid))
        await self.transport.started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_fetching_state_pins_work_and_clears_after_cancellation(self):
        aid = self.register()
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.store.read(MID, aid))
        await self.transport.started.wait()
        self.assertEqual(self.db.execute('SELECT state FROM message_attachments WHERE id=?', (aid,)).fetchone()[0], 'fetching')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.db.execute('SELECT state FROM message_attachments WHERE id=?', (aid,)).fetchone()[0], 'registered')
        self.assertFalse(self.db.in_transaction)

    async def test_status_counts_orphan_bytes_and_never_exposes_source(self):
        aid = self.register()
        await self.store.read(MID, aid)
        with self.db:
            self.db.execute('DELETE FROM messages WHERE id=?', (MID,))
        result = self.store.status()
        self.assertEqual(result['registered'], 0)
        self.assertEqual(result['cached_bytes'], len(PNG))
        self.assertEqual(result['cached_files'], 1)
        self.assertNotIn('secret', json.dumps(result))

    async def test_cache_reserve_evicts_before_staging_and_never_exceeds_cap(self):
        self.transport.body = b'a' * 80
        aid = self.register(kind='file')
        with patch.object(module, 'MAX_FILE_BYTES', 100), patch.object(module, 'MAX_CACHE_BYTES', 150):
            await self.store.read(MID, aid)
            other_aid = self.register(kind='file', mid=OTHER)
            await self.store.read(OTHER, other_aid)
        self.assertEqual(sum(path.stat().st_size for path in self.directory.iterdir()), 80)
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')
        self.assertEqual(self.store.list(OTHER)[0]['state'], 'cached')

    async def test_corrupt_cache_and_hardlink_are_rejected(self):
        aid = self.register()
        await self.store.read(MID, aid)
        cached = self.directory / (aid + '.bin')
        original = cached.read_bytes()
        cached.write_bytes(b'x' * len(original))
        with self.assertRaisesRegex(AttachmentError, '^attachment_cache_corrupt$'):
            await self.store.read(MID, aid)
        cached.write_bytes(original)
        os.link(cached, Path(self.temp.name) / 'hardlink')
        with self.assertRaisesRegex(AttachmentError, '^attachment_cache_unsafe$'):
            await self.store.read(MID, aid)

    async def test_symlink_destination_never_reads_or_overwrites_external_target(self):
        aid = self.register()
        outside = Path(self.temp.name) / 'outside'
        outside.write_bytes(b'unchanged')
        (self.directory / (aid + '.bin')).symlink_to(outside)
        with self.assertRaisesRegex(AttachmentError, '^attachment_cache_unsafe$'):
            await self.store.read(MID, aid)
        self.assertEqual(outside.read_bytes(), b'unchanged')
        self.assertEqual(self.transport.calls, [])

    async def test_symlink_ancestor_and_world_readable_directory_rejected(self):
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.directory, target_is_directory=True)
        for path in (link / 'sub',):
            with self.assertRaisesRegex(AttachmentError, '^attachment_cache_unsafe$'):
                Attachments(self.db, path, transport=FakeTransport())
        public = Path(self.temp.name) / 'public'
        public.mkdir(mode=0o755)
        with self.assertRaisesRegex(AttachmentError, '^attachment_cache_unsafe$'):
            Attachments(self.db, public, transport=FakeTransport())

    async def test_restart_cleans_only_owned_orphans_and_missing_metadata(self):
        aid = self.register()
        await self.store.read(MID, aid)
        (self.directory / (aid + '.bin')).unlink()
        orphan = self.directory / ('part_' + 'a' * 32 + '.tmp')
        orphan.write_bytes(b'partial')
        orphan.chmod(0o600)
        restarted = Attachments(self.db, self.directory, transport=FakeTransport())
        self.addAsyncCleanup(restarted.close)
        self.assertEqual(restarted.list(MID)[0]['state'], 'unfetched')
        self.assertEqual(list(self.directory.iterdir()), [])


class AttachmentTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, proxy=None):
        result = AttachmentTransport(proxy=proxy)
        self.addAsyncCleanup(result.close)
        return result

    async def test_url_schemes_literals_ports_credentials_and_controls_rejected(self):
        transport = self.transport()
        for url in ('file:///tmp/a', 'data:text/plain,a', 'base64://abc', '/tmp/a',
                    'http://media.example.com/f', 'https://127.0.0.1/f', 'https://[::1]/f',
                    'https://media.example.com:444/f', 'https://user:secret@media.example.com/f',
                    'https://media.example.com/#secret', 'https://media.example.com./f',
                    'https://media%2eexample.com/f', 'https://media.example.com\\@evil.test/f',
                    'https://media.example.com/f\n'):
            with self.subTest(url=url), self.assertRaises(AttachmentError):
                transport.validate(url)
        self.assertEqual(transport.validate(URL).hostname, 'media.example.com')

    async def test_dns_all_answers_must_be_public_and_connection_is_pinned(self):
        transport = self.transport()
        loop = asyncio.get_running_loop()
        for ips in (['127.0.0.1'], ['8.8.8.8', '10.0.0.2'], ['169.254.169.254'],
                    ['::1'], ['::ffff:8.8.8.8'], ['100.64.0.1'], ['2002:0808:0808::'], ['192.0.2.1']):
            with self.subTest(ips=ips), patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[dns_entry(ip) for ip in ips])):
                with self.assertRaisesRegex(AttachmentError, '^attachment_dns_blocked$'):
                    await transport._addresses('media.example.com')
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=[dns_entry()])):
            values = await transport._addresses('media.example.com')
        resolver = module._PinnedResolver('media.example.com', values)
        self.assertEqual(await resolver.resolve('media.example.com'), [entry()])
        with self.assertRaises(AttachmentError):
            await resolver.resolve('other.example.com')
        with self.assertRaises(AttachmentError):
            await resolver.resolve('media.example.com', 80)

    async def test_direct_get_no_redirect_cookies_ambient_proxy_or_decompression(self):
        transport = self.transport()
        session, context = fake_session()
        chunks = []
        with patch.object(transport, '_addresses', AsyncMock(return_value=[entry()])), \
             patch.object(module.aiohttp, 'TCPConnector') as connector, \
             patch.object(module.aiohttp, 'ClientSession', return_value=context) as client:
            await transport.download(URL, chunks.append)
        self.assertEqual(chunks, [b'bytes'])
        self.assertEqual(str(session.get.call_args.args[0]), URL)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.get.call_args.kwargs['headers']['Accept-Encoding'], 'identity')
        self.assertNotIn('proxy', session.get.call_args.kwargs)
        self.assertEqual(connector.call_args.kwargs['resolver'].entries, [entry()])
        self.assertFalse(connector.call_args.kwargs['use_dns_cache'])
        self.assertFalse(client.call_args.kwargs['trust_env'])
        self.assertFalse(client.call_args.kwargs['auto_decompress'])
        self.assertIsInstance(client.call_args.kwargs['cookie_jar'], aiohttp.DummyCookieJar)

    async def test_proxy_pins_target_keeps_tls_host_and_never_falls_back(self):
        proxy = proxy_module.ProxySettings(True, 'http://127.0.0.1:3128', 'username', 'password')
        transport = self.transport(proxy)
        for error in (None, aiohttp.ClientConnectionError('username:password secret URL')):
            session, context = fake_session(error=error)
            with patch.object(transport, '_addresses', AsyncMock(return_value=[entry()])), \
                 patch.object(module.aiohttp, 'TCPConnector') as connector, \
                 patch.object(module.aiohttp, 'ClientSession', return_value=context):
                if error:
                    with self.assertRaisesRegex(AttachmentError, '^attachment_network_failed$'):
                        await transport.download(URL, lambda chunk: None)
                else:
                    await transport.download(URL, lambda chunk: None)
            options = session.get.call_args.kwargs
            self.assertEqual(str(session.get.call_args.args[0]), 'https://8.8.8.8/a%2fb?secret=private%2Ftoken')
            self.assertEqual(options['server_hostname'], 'media.example.com')
            self.assertEqual(options['headers']['Host'], 'media.example.com')
            self.assertEqual(options['proxy'], proxy.url)
            self.assertEqual(options['proxy_auth'], aiohttp.BasicAuth('username', 'password'))
            self.assertIsNone(connector.call_args.kwargs['resolver'])
            self.assertEqual(session.get.call_count, 1)

    async def test_redirect_status_encoding_empty_and_size_failures(self):
        transport = self.transport()
        cases = [({'status': 302}, 'attachment_redirect_blocked'),
                 ({'status': 404}, 'attachment_http_failed'),
                 ({'headers': {'Content-Encoding': 'gzip'}}, 'attachment_encoding_unsupported'),
                 ({'chunks': ()}, 'attachment_empty'),
                 ({'length': module.MAX_FILE_BYTES + 1}, 'attachment_too_large'),
                 ({'chunks': (b'x' * (module.MAX_FILE_BYTES + 1),)}, 'attachment_too_large')]
        for arguments, code in cases:
            with self.subTest(code=code):
                session, context = fake_session(**arguments)
                with patch.object(transport, '_addresses', AsyncMock(return_value=[entry()])), \
                     patch.object(module.aiohttp, 'TCPConnector'), \
                     patch.object(module.aiohttp, 'ClientSession', return_value=context):
                    with self.assertRaisesRegex(AttachmentError, '^' + code + '$'):
                        await transport.download(URL, lambda chunk: None)
                self.assertEqual(session.get.call_count, 1)

    async def test_authorization_guard_runs_after_dns_before_http_request(self):
        transport = self.transport()
        state = []
        async def dns(_host):
            state.append('dns finished')
            return [entry()]
        def guard():
            self.assertEqual(state, ['dns finished'])
            raise AttachmentError('attachment_source_unavailable')
        with patch.object(transport, '_addresses', dns), \
             patch.object(module.aiohttp, 'ClientSession') as client:
            with self.assertRaisesRegex(AttachmentError, '^attachment_source_unavailable$'):
                await transport.download(URL, lambda chunk: None, guard=guard)
        client.assert_not_called()

    async def test_stopped_transport_and_timeout_are_fixed_safe_errors(self):
        transport = self.transport()
        with patch.object(transport, '_download', AsyncMock(side_effect=asyncio.TimeoutError('secret'))):
            with self.assertRaisesRegex(AttachmentError, '^attachment_timeout$'):
                await transport.download(URL, lambda chunk: None)
        await transport.close()
        with self.assertRaisesRegex(AttachmentError, '^attachment_stopping$'):
            await transport.download(URL, lambda chunk: None)


class AttachmentSourceDiagnosticTests(unittest.TestCase):
    def test_no_raw_url_data_in_shape(self):
        for url, host, port in [('http://u:SECRET@files.example/a?SECRET', 'files.example', 80),
                                ('http://192.168.1.2:8080/SECRET', '192.168.1.2', 8080),
                                ('http://[::1]/SECRET', '::1', 80),
                                ('http://bad_host/SECRET', None, 80),
                                ('http://host:BAD/SECRET', None, None)]:
            with self.subTest(url=url):
                error = AttachmentError('attachment_https_required')
                error.note_http_api_source(url)
                self.assertEqual(error.source_diagnostic, {'source': 'adapter_api', 'scheme': 'http',
                                                           'hostname': host, 'port': port})
                self.assertNotIn('SECRET', json.dumps(error.source_diagnostic))

    def test_no_diagnostic_for_other_errors_or_https(self):
        for code, url in [('attachment_url_invalid', 'http://host/SECRET'),
                          ('attachment_https_required', 'https://host/SECRET')]:
            error = AttachmentError(code); error.note_http_api_source(url)
            self.assertIsNone(error.source_diagnostic)


if __name__ == '__main__':
    unittest.main()
