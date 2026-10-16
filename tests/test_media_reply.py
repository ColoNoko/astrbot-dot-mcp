"""Outbound media validation and fake-bot tests; no QQ or external connections."""

import asyncio
import base64
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
media = importlib.import_module('astrbot_dot_plugin.media_reply')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=')


def encoded(data):
    return base64.b64encode(data).decode('ascii')


class MediaValidationTests(unittest.TestCase):
    def test_metadata_fingerprint_and_no_retained_decoded_bytes(self):
        value = media.validate_media('image', '图片.png', encoded(PNG))
        self.assertEqual(value.kind, 'image')
        self.assertEqual(value.mime_type, 'image/png')
        self.assertEqual(value.size_bytes, len(PNG))
        self.assertEqual(value.sha256, hashlib.sha256(PNG).hexdigest())
        fingerprint = json.loads(value.fingerprint)
        self.assertEqual(fingerprint, {'v': 1, 'kind': 'image', 'filename': '图片.png',
                                      'size_bytes': len(PNG), 'sha256': value.sha256})
        self.assertNotIn(value.data_base64, value.fingerprint)
        self.assertNotIn(value.data_base64, repr(value))
        self.assertFalse(any(isinstance(item, bytes) for item in vars(value).values()))
        self.assertEqual(media.validate_media('image', '图片.png', encoded(PNG)).fingerprint,
                         value.fingerprint)
        with self.assertRaises(Exception):
            value.filename = 'new.png'

    def test_fingerprint_covers_kind_name_size_and_bytes(self):
        value = media.validate_media('file', 'file.bin', encoded(PNG))
        others = [media.validate_media('image', 'file.bin', encoded(PNG)),
                  media.validate_media('file', 'other.bin', encoded(PNG)),
                  media.validate_media('file', 'file.bin', encoded(PNG + b'x')),
                  media.validate_media('file', 'file.bin', encoded(b'x' * len(PNG)))]
        self.assertEqual(len({value.fingerprint, *(v.fingerprint for v in others)}), 5)

    def test_reject_noncanonical_base64_and_sources(self):
        invalid = ['', None, True, b'Zg==', 'Zg', 'Zg=', 'Zg===', 'Zh==', 'Zm9=',
                   'Zg==\n', 'Z g==', 'Zm-8', 'Zm_8', '☃===', '\ud800===',
                   'base64://Zg==', 'data:text/plain;base64,Zg==', 'https://example.com/file',
                   'file:///etc/passwd', '/etc/passwd', 'AAAA====', '====']
        for data in invalid:
            with self.subTest(data=repr(data)), self.assertRaises(media.Rejected):
                media.validate_media('file', 'file.txt', data)

    def test_canonical_all_padding_forms(self):
        for data in (b'f', b'fo', b'foo', bytes(range(256))):
            value = media.validate_media('file', 'sample.bin', encoded(data))
            self.assertEqual(value.size_bytes, len(data))
            self.assertEqual(value.data_base64, encoded(data))

    def test_unsafe_names_rejected_without_silent_path_truncation(self):
        invalid = ['', '.', '..', '../a', '/tmp/a', 'C:\\a', 'a/b', 'a\\b', 'a:b',
                   'a\x00b', 'a\nb', 'a\u202eb', 'a\ud800b', 'name.', ' name', 'name ',
                   'a|b', 'a?b', 'a"b', 'a<b', 'a>b', 'a*b', 'NUL', 'CON.txt',
                   'com1.log', 'lpt9', 'a' * 181, '中' * 61, 'a／b', 'a＼b', None, True]
        for name in invalid:
            with self.subTest(name=repr(name)), self.assertRaisesRegex(media.Rejected, '^invalid_media_filename$'):
                media.validate_media('file', name, 'Zg==')
        self.assertEqual(media.validate_media('file', '文档 2026.txt', 'Zg==').filename,
                         '文档 2026.txt')
        self.assertEqual(media.validate_media('file', 'ａ.txt', 'Zg==').filename, 'a.txt')

    def test_only_image_magic_is_classified_never_rendered(self):
        allowed = [PNG, b'\xff\xd8\xff\xe0hello', b'GIF89a\x01\x00\x01\x00rest',
                   b'RIFF\x08\x00\x00\x00WEBPVP8 hello']
        for data in allowed:
            self.assertTrue(media.validate_media('image', 'picture.dat', encoded(data)).mime_type.startswith('image/'))
        for data in (b'MZ executable', b'%PDF-1.7', b'<svg/>', b'BM...', b'\x89PNG\r\n\x1a\n'):
            with self.assertRaisesRegex(media.Rejected, '^unsupported_media_image$'):
                media.validate_media('image', 'picture.png', encoded(data))
            self.assertEqual(media.validate_media('file', 'file.bin', encoded(data)).size_bytes, len(data))
        for kind in ('video', 'record', 'text', None, True, []):
            with self.assertRaisesRegex(media.Rejected, '^invalid_media_kind$'):
                media.validate_media(kind, 'file.bin', 'Zg==')

    def test_size_boundary_and_predecode_rejection(self):
        data = b'x' * media.MAX_MEDIA_BYTES
        value = media.validate_media('file', 'limit.bin', encoded(data))
        self.assertEqual(value.size_bytes, 20_000_000)
        for data in (encoded(data + b'x'), 'A' * (media.MAX_BASE64_CHARS + 4)):
            with patch.object(media.base64, 'b64decode', side_effect=AssertionError('must not decode')):
                with self.assertRaisesRegex(media.Rejected, '^media_too_large$'):
                    media.validate_media('file', 'oversize.bin', data)


class FakeBot:
    def __init__(self, result=None):
        self.result = {'message_id': 456} if result is None else result
        self.calls = []
        self.error = None
        self.wait = None
        self.entered = asyncio.Event()

    async def call_action(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.wait is not None:
            await self.wait.wait()
        if self.error:
            raise self.error
        return self.result


def target(group=False):
    bot = FakeBot()
    raw = {'post_type': 'message', 'message_type': 'group' if group else 'private',
           'self_id': 100, 'user_id': 200, 'sender': {'user_id': 200}, 'message_id': 'original-1'}
    if group:
        raw['group_id'] = 300
    event = types.SimpleNamespace(bot=bot, message_obj=types.SimpleNamespace(raw_message=raw))
    row = {'source_id': 'original-1', 'conversation_type': 'group' if group else 'private',
           'group_id': '300' if group else '', 'sender_id': '200'}
    return event, row, bot


class MediaSendTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, event, row, bot, value=None, guard=None, bound_self_id='100'):
        value = value or media.validate_media('file', 'file.bin', encoded(b'new bytes'))
        return await media.send_media(event, row, value, bound_self_id=bound_self_id,
                                     captured_bot=bot, guard=guard or (lambda: event))

    async def test_private_and_group_image_and_file_single_exact_call(self):
        for group in (False, True):
            for kind in ('image', 'file'):
                event, row, bot = target(group)
                value = media.validate_media(kind, 'new.png', encoded(PNG))
                self.assertTrue(await self.invoke(event, row, bot, value))
                self.assertEqual(bot.calls, [{'action': 'send_group_msg' if group else 'send_private_msg',
                    'group_id' if group else 'user_id': 300 if group else 200,
                    'self_id': 100, 'message': [{'type': kind, 'data': {
                        'name': 'new.png', 'file': 'base64://' + encoded(PNG)}}]}])

    async def test_route_mismatch_fails_closed_before_adapter(self):
        changes = [('self_id', 999), ('self_id', True), ('user_id', 999), ('user_id', True),
                   ('group_id', 300), ('post_type', 'notice'), ('message_type', 'group'),
                   ('message_id', 'other'), ('sender', {'user_id': 999})]
        for key, value in changes:
            event, row, bot = target()
            event.message_obj.raw_message[key] = value
            with self.subTest(key=key), self.assertRaises(media.NotSent):
                await self.invoke(event, row, bot)
            self.assertEqual(bot.calls, [])
        for key, value in [('group_id', '999'), ('group_id', ''), ('conversation_type', 'private')]:
            event, row, bot = target(True)
            row[key] = value
            with self.assertRaises(media.NotSent):
                await self.invoke(event, row, bot)
            self.assertEqual(bot.calls, [])

    async def test_guard_revocation_event_or_bot_replacement_never_sends(self):
        def revoked():
            raise media.Rejected('binding_inactive')
        for guard in (revoked, lambda: None, lambda: types.SimpleNamespace()):
            event, row, bot = target()
            with self.assertRaises(media.NotSent):
                await self.invoke(event, row, bot, guard=guard)
            self.assertEqual(bot.calls, [])
        event, row, original = target()
        replacement = event.bot = FakeBot()
        with self.assertRaises(media.NotSent):
            await self.invoke(event, row, original)
        self.assertEqual(original.calls + replacement.calls, [])

    async def test_result_requires_explicit_valid_message_id(self):
        good = [{'message_id': 1}, {'message_id': -123}, {'message_id': '123'},
                {'message_id': '-123'}, {'message_id': 123, 'status': 'ok', 'retcode': 0}]
        bad = [None, {}, False, True, 123, {'message_id': None}, {'message_id': 0},
               {'message_id': True}, {'message_id': '0'}, {'message_id': '+123'},
               {'message_id': '01'}, {'message_id': 'unsafe'}, {'message_id': '1\n'},
               {'message_id': 2 ** 63}, {'data': {'message_id': 123}},
               {'message_id': 123, 'status': 'failed'}, {'message_id': 123, 'retcode': 1}]
        for result, expected in [(r, True) for r in good] + [(r, False) for r in bad]:
            event, row, bot = target()
            bot.result = result
            self.assertEqual(await self.invoke(event, row, bot), expected, result)
            self.assertEqual(len(bot.calls), 1)

    async def test_adapter_error_and_cancellation_propagate_without_retry(self):
        event, row, bot = target()
        bot.error = RuntimeError('private failure')
        with self.assertRaises(RuntimeError):
            await self.invoke(event, row, bot)
        self.assertEqual(len(bot.calls), 1)
        event, row, bot = target()
        bot.wait = asyncio.Event()
        task = asyncio.create_task(self.invoke(event, row, bot))
        await bot.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(bot.calls), 1)

    async def test_guard_and_api_entry_have_no_scheduling_gap(self):
        event, row, bot = target()
        order = []
        def guard():
            order.append('guard')
            asyncio.get_running_loop().call_soon(order.append, 'scheduled')
            return event
        async def call_action(**kwargs):
            order.append('adapter')
            return {'message_id': 123}
        bot.call_action = call_action
        self.assertTrue(await self.invoke(event, row, bot, guard=guard))
        self.assertEqual(order, ['guard', 'adapter'])


if __name__ == '__main__':
    unittest.main()
