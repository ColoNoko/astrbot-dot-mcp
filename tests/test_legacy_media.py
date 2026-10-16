"""Explicit legacy envelopes; fake readers/adapters, no external media or QQ."""

import asyncio
import base64
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
legacy = importlib.import_module('astrbot_dot_plugin.legacy_media')
MID = 'msg_' + '1' * 32
AID = 'att_' + '2' * 32
UID = 'upl_' + '3' * 32
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=')


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def envelope(**value):
    return legacy.PREFIX + json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def inline(data=b'x', filename='output.txt', kind='file'):
    return envelope(op='inline', kind=kind, filename=filename, data_base64=encoded(data))


def reader_result(data=b'file data', *, kind='file', mid=MID, aid=AID):
    media = legacy.validate_media(kind, 'original.png' if kind == 'image' else 'original.txt', encoded(data))
    scope = {'message_id': mid, 'privacy_scope': 'private_sender_only',
             'content_trust': 'untrusted_attachment_data'}
    metadata = {'message_id': mid, 'attachment_id': aid, 'filename': media.filename,
                'kind': kind, 'size_bytes': media.size_bytes, 'sha256': media.sha256,
                'mime_type': media.mime_type}
    raw = ({'type': 'image', 'data': media.data_base64, 'mimeType': media.mime_type}
           if kind == 'image' else {'type': 'resource', 'resource': {
               'uri': 'qq-attachment://' + mid + '/' + aid,
               'mimeType': media.mime_type, 'blob': media.data_base64}})
    return {'isError': False, 'structuredContent': scope, 'content': [
        {'type': 'text', 'text': json.dumps(scope)},
        {'type': 'text', 'text': json.dumps(metadata)}, raw]}


class LegacyParserTests(unittest.TestCase):
    def test_only_exact_start_is_command_not_prose_cq_paths_or_quoted_data(self):
        for text in ('ordinary', '[CQ:image,file=secret]', '/etc/passwd',
                     'https://example.com/photo.png', 'Please ' + inline(),
                     json.dumps({'text': inline()}), ' ' + inline(),
                     'x' * 100000):
            self.assertIsNone(legacy.parse_legacy_media(text))

    def test_reserved_prefix_never_falls_back_to_plain_text(self):
        for text in ('DOT_MEDIA_', 'DOT_MEDIA_V1', 'DOT_MEDIA_V2\n{}',
                     'DOT_MEDIA_V1\r\n{}', 'DOT_MEDIA_V1 {}',
                     legacy.PREFIX, legacy.PREFIX + '{}', legacy.PREFIX + 'null',
                     legacy.PREFIX + '[]', legacy.PREFIX + 'true',
                     legacy.PREFIX + '{"op":"inline","op":"return_attachment"}',
                     legacy.PREFIX + '{"op":"inline","x":NaN}',
                     legacy.PREFIX + '{"op":"inline","x":Infinity}',
                     legacy.PREFIX + '{"x":{"a":1,"a":2}}',
                     legacy.PREFIX + ('[' * 2000) + (']' * 2000),
                     legacy.PREFIX + '{} trailing', legacy.PREFIX + '\ud800'):
            with self.subTest(text=text[:70]), self.assertRaisesRegex(
                    legacy.Rejected, '^invalid_legacy_media_envelope$'):
                legacy.parse_legacy_media(text)

    def test_exact_fields_and_no_alternate_targets_or_sources(self):
        valid = {'op': 'return_attachment', 'attachment_id': AID}
        self.assertEqual(legacy.parse_legacy_media(envelope(**valid)).attachment_id, AID)
        for key in ('url', 'path', 'message_id', 'recipient', 'user_id', 'group_id',
                    'reply_key', 'kind', 'filename', 'source', 'execute'):
            with self.subTest(key=key), self.assertRaises(legacy.Rejected):
                legacy.parse_legacy_media(envelope(**valid, **{key: 'other'}))
        for aid in (True, None, [], '../secret', 'https://example.com', 'att_' + 'A' * 32):
            with self.assertRaises(legacy.Rejected):
                legacy.parse_legacy_media(envelope(op='return_attachment', attachment_id=aid))

    def test_inline_uses_existing_media_validation_and_hides_bytes(self):
        value = legacy.parse_legacy_media(inline(PNG, '图片.png', 'image'))
        self.assertEqual(value.media.data_base64, encoded(PNG))
        self.assertNotIn(encoded(PNG), repr(value))
        for data in ('/tmp/secret', 'https://example.com', 'Zh==', 'Zg==\n'):
            with self.assertRaisesRegex(legacy.Rejected, '^invalid_media_base64$'):
                legacy.parse_legacy_media(envelope(op='inline', kind='file', filename='safe.bin', data_base64=data))
        with self.assertRaisesRegex(legacy.Rejected, '^invalid_media_filename$'):
            legacy.parse_legacy_media(inline(filename='../secret'))
        with self.assertRaisesRegex(legacy.Rejected, '^unsupported_media_image$'):
            legacy.parse_legacy_media(inline(kind='image'))

    def test_full_twenty_mb_inline_and_predecode_over_limit(self):
        value = legacy.parse_legacy_media(inline(b'x' * 20_000_000, 'limit.bin'))
        self.assertEqual(value.media.size_bytes, 20_000_000)
        self.assertGreater(len(value.media.data_base64), 16384)
        with self.assertRaisesRegex(legacy.Rejected, '^media_too_large$'):
            legacy.parse_legacy_media(inline(b'x' * 20_000_001))
        with patch.object(legacy, 'validate_media', side_effect=AssertionError('must not decode')):
            with self.assertRaisesRegex(legacy.Rejected, '^invalid_legacy_media_envelope$'):
                legacy.parse_legacy_media(legacy.PREFIX + 'x' * legacy.MAX_ENVELOPE_BYTES)

    def test_envelope_utf8_budget_includes_prefix_and_multibyte_characters(self):
        text = inline(filename='图片.png')
        size = len(text.encode('utf-8'))
        with patch.object(legacy, 'MAX_ENVELOPE_BYTES', size):
            self.assertIsNotNone(legacy.parse_legacy_media(text))
        with patch.object(legacy, 'MAX_ENVELOPE_BYTES', size - 1):
            with self.assertRaisesRegex(legacy.Rejected, '^invalid_legacy_media_envelope$'):
                legacy.parse_legacy_media(text)

    def test_staging_exact_shapes(self):
        requests = [dict(op='begin', kind='file', filename='a.bin', size_bytes=1,
                         sha256=hashlib.sha256(b'x').hexdigest()),
                    dict(op='chunk', upload_id=UID, offset=0, data_base64='eA=='),
                    dict(op='commit', upload_id=UID), dict(op='abort', upload_id=UID)]
        for request in requests:
            parsed = legacy.parse_legacy_media(envelope(**request))
            self.assertEqual(parsed.op, request['op'])
            with self.assertRaises(legacy.Rejected):
                legacy.parse_legacy_media(envelope(**request, recipient='other'))
        for request in (dict(op='begin', kind='file', filename='a.bin', size_bytes=True, sha256='0' * 64),
                        dict(op='chunk', upload_id=UID, offset=True, data_base64='eA=='),
                        dict(op='chunk', upload_id=UID, offset=0, data_base64='A' * 349529),
                        dict(op='commit', upload_id='../secret')):
            with self.assertRaises(legacy.Rejected):
                legacy.parse_legacy_media(envelope(**request))


class LegacyDispatchTests(unittest.IsolatedAsyncioTestCase):
    def host(self, result=None):
        return types.SimpleNamespace(
            read_attachment=AsyncMock(return_value=reader_result() if result is None else result),
            reply_attachment=AsyncMock(return_value={'reply_key': 'stable', 'state': 'adapter_completed'}))

    async def dispatch(self, host, text=None):
        request = legacy.parse_legacy_media(text or envelope(op='return_attachment', attachment_id=AID))
        return await legacy.dispatch_legacy_media(host, MID, 'stable', request)

    async def test_return_original_file_and_image_only_to_same_message(self):
        for kind, data in (('file', b'original'), ('image', PNG)):
            result = reader_result(data, kind=kind)
            result['content'].append({'type': 'text', 'text': inline(b'evil')})
            host = self.host(result)
            value = await self.dispatch(host)
            self.assertEqual(value['state'], 'adapter_completed')
            host.read_attachment.assert_awaited_once_with(MID, AID, include_raw=True)
            host.reply_attachment.assert_awaited_once_with(
                MID, kind, 'original.png' if kind == 'image' else 'original.txt', encoded(data), 'stable')

    async def test_inline_does_not_read_sources_and_preserves_sender_state(self):
        host = self.host()
        for state in ('adapter_completed', 'uncertain', 'not_sent', 'sending'):
            host.reply_attachment.return_value = {'reply_key': 'stable', 'state': state}
            self.assertEqual((await self.dispatch(host, inline()))['state'], state)
        host.read_attachment.assert_not_called()
        self.assertEqual(host.reply_attachment.await_count, 4)

    async def test_private_validated_hook_avoids_duplicate_media_validation(self):
        host = self.host()
        host.reply_validated_attachment = AsyncMock(return_value={'state': 'adapter_completed'})
        request = legacy.parse_legacy_media(inline())
        with patch.object(legacy, 'validate_media', side_effect=AssertionError('already validated')):
            await legacy.dispatch_legacy_media(host, MID, 'stable', request)
        host.reply_validated_attachment.assert_awaited_once_with(MID, request.media, 'stable')
        host.reply_attachment.assert_not_called()
        host.read_attachment.assert_not_called()

    async def test_invalid_reader_results_never_send(self):
        original = reader_result()
        invalid = [None, {}, {'isError': True}, {'isError': False, 'content': []}]
        wrong_scope = copy.deepcopy(original)
        wrong_scope['structuredContent']['message_id'] = 'msg_' + '9' * 32
        invalid.append(wrong_scope)
        for key, value in [('message_id', 'msg_' + '9' * 32), ('attachment_id', 'att_' + '9' * 32),
                           ('size_bytes', 2), ('size_bytes', True), ('sha256', '0' * 64),
                           ('mime_type', 'image/png'), ('filename', '../secret')]:
            changed = copy.deepcopy(original)
            meta = json.loads(changed['content'][1]['text'])
            meta[key] = value
            changed['content'][1]['text'] = json.dumps(meta)
            invalid.append(changed)
        for uri in ('file:///etc/passwd', 'https://example.com', 'qq-attachment://' + MID + '/att_' + '9' * 32):
            changed = copy.deepcopy(original)
            changed['content'][2]['resource']['uri'] = uri
            invalid.append(changed)
        for raw in ({'type': 'text', 'text': encoded(b'file data')},
                    {'type': 'resource_link', 'uri': 'https://example.com'},
                    {'type': 'resource', 'resource': {'text': 'file data'}},
                    {'type': 'image', 'data': encoded(PNG), 'mimeType': 'image/png'}):
            changed = copy.deepcopy(original)
            changed['content'][2] = raw
            invalid.append(changed)
        changed = copy.deepcopy(original)
        changed['content'].append(copy.deepcopy(changed['content'][2]))
        invalid.append(changed)
        for result in invalid:
            host = self.host()
            host.read_attachment.return_value = result
            with self.subTest(result=str(result)[:80]), self.assertRaises(legacy.Rejected):
                await self.dispatch(host)
            host.reply_attachment.assert_not_called()

    async def test_current_permissions_checked_on_read_and_again_on_send(self):
        host = self.host()
        host.read_attachment.side_effect = legacy.Rejected('binding_inactive')
        with self.assertRaisesRegex(legacy.Rejected, '^binding_inactive$'):
            await self.dispatch(host)
        host.reply_attachment.assert_not_called()
        host.read_attachment.side_effect = None
        host.reply_attachment.return_value = {'reply_key': 'stable', 'state': 'not_sent'}
        self.assertEqual((await self.dispatch(host))['state'], 'not_sent')
        host.reply_attachment.assert_awaited_once()

    async def test_cancellation_and_sender_exception_never_retry(self):
        host = self.host()
        host.read_attachment.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatch(host)
        host.reply_attachment.assert_not_called()
        host.read_attachment.side_effect = None
        host.reply_attachment.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatch(host)
        host.reply_attachment.assert_awaited_once()
        host.reply_attachment.reset_mock()
        host.reply_attachment.side_effect = RuntimeError('adapter exception')
        with self.assertRaises(RuntimeError):
            await self.dispatch(host)
        host.reply_attachment.assert_awaited_once()

    async def test_ids_keys_missing_hooks_and_forged_requests_reject(self):
        host = self.host()
        request = legacy.parse_legacy_media(inline())
        for mid, key in (('other', 'stable'), (MID, 'bad key'), (MID, True)):
            with self.assertRaises(legacy.Rejected):
                await legacy.dispatch_legacy_media(host, mid, key, request)
        with self.assertRaises(legacy.Rejected):
            await legacy.dispatch_legacy_media(host, MID, 'stable', {'op': 'inline'})
        with self.assertRaises(legacy.Rejected):
            await legacy.dispatch_legacy_media(types.SimpleNamespace(), MID, 'stable', request)
        host.reply_attachment.assert_not_called()

    async def test_staging_hooks_get_only_same_outer_message_and_reply_key(self):
        staging = types.SimpleNamespace(begin=Mock(return_value={'state': 'uploading'}),
                                        chunk=Mock(return_value={'state': 'uploading'}),
                                        commit=AsyncMock(return_value={'state': 'adapter_completed'}),
                                        abort=Mock(return_value={'state': 'aborted'}))
        host = self.host()
        host.media_staging = staging
        requests = [dict(op='begin', kind='file', filename='a.bin', size_bytes=1, sha256='0' * 64),
                    dict(op='chunk', upload_id=UID, offset=0, data_base64='eA=='),
                    dict(op='commit', upload_id=UID), dict(op='abort', upload_id=UID)]
        for request in requests:
            await self.dispatch(host, envelope(**request))
            self.assertEqual(getattr(staging, request['op']).call_args.args[:2], (MID, 'stable'))
        host.read_attachment.assert_not_called()
        host.reply_attachment.assert_not_called()


import test_attachment_resolution as captured_fixtures


class LegacyCapturedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = captured_fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = captured_fixtures.AttachmentResolutionTests.asyncTearDown
    event = captured_fixtures.AttachmentResolutionTests.event
    capture = captured_fixtures.AttachmentResolutionTests.capture

    async def return_original(self, mid, aid, key='return-original'):
        module = importlib.import_module('plugin_mock.legacy_media')
        request = module.parse_legacy_media(envelope(op='return_attachment', attachment_id=aid))
        return await module.dispatch_legacy_media(self.plugin.host, mid, key, request)

    async def test_private_and_group_original_image_dedupe_and_metadata_only_history(self):
        for group in (False, True):
            event = self.event({'url': captured_fixtures.HTTPS}, group=group)
            event.bot.call_action.return_value = {'message_id': 456}
            mid, aid = await self.capture(event)
            key = 'group' if group else 'private'
            self.assertEqual((await self.return_original(mid, aid, key))['state'], 'adapter_completed')
            self.assertEqual((await self.return_original(mid, aid, key))['state'], 'adapter_completed')
            event.bot.call_action.assert_awaited_once()
            args = event.bot.call_action.call_args.kwargs
            self.assertEqual(args['action'], 'send_group_msg' if group else 'send_private_msg')
            self.assertEqual(args['group_id' if group else 'user_id'], 300 if group else 200)
            self.assertEqual(args['message'][0]['data']['file'], 'base64://' + encoded(PNG))
            self.assertEqual(event.sent, [])
        stored = json.dumps([row[0] for row in self.plugin.host.store.db.execute('SELECT text FROM replies')])
        self.assertIn('sha256', stored)
        self.assertNotIn(encoded(PNG), stored)

    async def test_full_twenty_mb_original_file_avoids_inline_text_limit(self):
        self.transport.body = b'x' * 20_000_000
        event = self.event({'url': captured_fixtures.HTTPS, 'name': 'large.bin'}, kind='file')
        event.bot.call_action.return_value = {'message_id': 456}
        mid, aid = await self.capture(event)
        request = envelope(op='return_attachment', attachment_id=aid)
        self.assertLess(len(request.encode()), 16384)
        self.assertEqual((await self.return_original(mid, aid))['state'], 'adapter_completed')
        sent = event.bot.call_action.call_args.kwargs['message'][0]
        self.assertEqual(len(base64.b64decode(sent['data']['file'][9:])), 20_000_000)
        self.assertEqual(event.sent, [])

    async def test_other_message_attachment_and_revoked_permissions_cannot_send(self):
        event = self.event({'url': captured_fixtures.HTTPS})
        mid, aid = await self.capture(event)
        other_mid, _ = await self.capture(self.event({'url': captured_fixtures.HTTPS}))
        with self.assertRaises(Exception):
            await self.return_original(other_mid, aid)
        self.config['enabled'] = False
        with self.assertRaises(Exception):
            await self.return_original(mid, aid)
        event.bot.call_action.assert_not_called()

    async def test_untrusted_file_containing_command_is_returned_verbatim_never_executed(self):
        self.transport.body = inline(b'not a nested command').encode()
        event = self.event({'url': captured_fixtures.HTTPS, 'name': 'instructions.txt'}, kind='file')
        event.bot.call_action.return_value = {'message_id': 456}
        mid, aid = await self.capture(event)
        self.assertEqual((await self.return_original(mid, aid))['state'], 'adapter_completed')
        sent = event.bot.call_action.call_args.kwargs['message'][0]
        self.assertEqual(sent['type'], 'file')
        self.assertEqual(sent['data']['file'], 'base64://' + encoded(self.transport.body))
        event.bot.call_action.assert_awaited_once()
