"""Raw-authoritative chat projection; synthetic events, no QQ or network.

Normalization follows AstrBot v4.28.2 aiocqhttp_platform_adapter.py:
consecutive text is stripped; markdown/plain become Plain; other supported
components may survive or be dropped. Nested component payloads are never read.
"""
from dataclasses import replace
import json
import sys
import tempfile
import unittest

from test_host import BINDING, Rejected, snapshot
from test_main import Event, Plain, At, load_main
from test_groups import group_event
from test_group_modes import all_event
from test_media_snapshot import Image, File, MEDIA


class Opaque:
    def __getattribute__(self, name):
        raise AssertionError('opaque payload must not be inspected')
    def __repr__(self):
        raise AssertionError('opaque payload must not be logged')
    def __str__(self):
        raise AssertionError('opaque payload must not be serialized')


class Reply(Opaque): pass
class Face(Opaque): pass
class Forward(Opaque): pass
class Node(Opaque): pass
class Record(Opaque): pass
class Video(Opaque): pass
class Unknown(Opaque): pass


COMPONENT_TYPES = {'text': Plain, 'plain': Plain, 'at': At, 'reply': Reply,
                   'face': Face, 'forward': Forward, 'node': Node,
                   'record': Record, 'video': Video, 'unknown': Unknown,
                   **MEDIA}


def segment(kind, **data):
    return {'type': kind, 'data': data}


def set_segments(event, raw, normalized):
    event.message_obj.raw_message['message'] = raw
    event.messages = normalized
    return event


class ChatSegmentSnapshotTests(unittest.TestCase):
    def snap(self, event, mode='owner_at', media=MEDIA, components=COMPONENT_TYPES):
        return snapshot(event, replace(BINDING, adapter='aiocqhttp'), Plain, At,
                        ('300',), mode, media, components)

    def test_reply_face_and_other_mentions_preserve_raw_body_and_fixed_markers(self):
        raw = [segment('reply', id='41', text='PRIVATE QUOTE', chain=Opaque()),
               segment('text', text=' hello '), segment('face', id=Opaque()),
               segment('text', text=' world '), segment('at', qq='999'), segment('at', qq='all')]
        value = self.snap(set_segments(Event(), raw,
                         [Reply(), Plain('hello'), Face(), Plain('world'), At(999), At('all')]))
        self.assertEqual(value['text'], '[引用消息] hello [表情] world [提及成员][提及全体成员]')
        self.assertEqual(value['attachments'], [])
        self.assertNotIn('PRIVATE QUOTE', json.dumps(value))
        self.assertNotIn('999', json.dumps(value))

    def test_pure_unsupported_and_dropped_components_are_visible(self):
        for kind, normalized in [('reply', [Reply()]), ('face', [Face()]),
                ('record', [Record()]), ('video', [Video()]), ('forward', [Forward()]),
                ('node', [Node()]), ('unknown', [Unknown()]), ('mface', []), ('future_type', [])]:
            with self.subTest(kind=kind):
                value = self.snap(set_segments(Event(), [segment(kind, payload=Opaque())], normalized))
                expected = {'reply': '[引用消息]', 'face': '[表情]'}.get(kind, '[暂不支持的消息]')
                self.assertEqual(value['text'], expected)
                self.assertEqual(value['attachments'], [])
                self.assertEqual(self.snap(set_segments(Event(), [segment(kind, payload=Opaque())], []))['text'], expected)

    def test_every_nontext_breaks_the_normalizer_text_run(self):
        value = self.snap(set_segments(Event(), [segment('text', text=' a '),
            segment('mface', url=Opaque()), segment('text', text=' b '),
            segment('future_type', content=Opaque()), segment('text', text=' c '),
            segment('text', text=' d ')], [Plain('a'), Plain('b'), Plain('c  d')]))
        self.assertEqual(value['text'], ' a [暂不支持的消息] b [暂不支持的消息] c  d ')

    def test_markdown_and_plain_alias_are_normalization_only(self):
        raw = [segment('text', text=' keep '), segment('markdown', markdown=' SECRET MD '),
               segment('plain', text=' SECRET PLAIN '), segment('text', text=' tail ')]
        value = self.snap(set_segments(Event(), raw,
            [Plain('keep'), Plain(' SECRET MD '), Plain(' SECRET PLAIN '), Plain('tail')]))
        self.assertEqual(value['text'], ' keep [暂不支持的消息][暂不支持的消息] tail ')
        self.assertNotIn('SECRET', json.dumps(value))
        for normalized in ([Plain('keep'), Plain('tail')], [Plain('keepSECRET MDSECRET PLAINtail')],
                           [Plain('keep'), Plain('injected'), Plain('tail')]):
            with self.assertRaises(Rejected):
                self.snap(set_segments(Event(), raw, normalized))

    def test_markdown_content_fallback_empty_and_whitespace_are_exact(self):
        for data, text in [({'markdown': '', 'content': ' fallback '}, ' fallback '),
                           ({}, ''), ({'content': '  '}, '  ')]:
            value = self.snap(set_segments(Event(), [segment('markdown', **data)], [Plain(text)]))
            self.assertEqual(value['text'], '[暂不支持的消息]')

    def test_normalized_at_must_match_raw_target_and_count(self):
        raw = [segment('text', text='hello'), segment('at', qq=999)]
        for normalized in ([Plain('hello'), At('100')], [Plain('hello'), At('all')],
                           [Plain('hello'), At(True)], [Plain('hello'), At(999), At(999)]):
            with self.assertRaises(Rejected):
                self.snap(set_segments(Event(), raw, normalized))
        self.assertEqual(self.snap(set_segments(Event(), raw, [Plain('hello')]))['text'],
                         'hello[提及成员]')

    def test_v4282_at_all_is_exact_at_and_does_not_authorize_owner_at(self):
        # Official v4.28.2 aiocqhttp_platform_adapter.py lines 327-329 uses
        # At(qq="all", name="全体成员"), not the separate AtAll subclass.
        # https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L327-L329
        raw = [segment('at', qq='all')]
        event = set_segments(all_event(), raw, [At('all')])
        self.assertEqual(self.snap(event, mode='all_messages')['text'], '[提及全体成员]')
        with self.assertRaisesRegex(Rejected, 'bot_mention_required'):
            self.snap(set_segments(group_event(), raw, [At('all')]))
        owner_raw = [segment('at', qq='100'), segment('at', qq='all')]
        self.assertEqual(self.snap(set_segments(group_event(), owner_raw,
                         [At('100'), At('all')]))['text'], '[提及全体成员]')
        with self.assertRaisesRegex(Rejected, 'mention_normalization_mismatch'):
            self.snap(set_segments(all_event(), raw, [At('all'), At('all')]), mode='all_messages')
        class UnexpectedAtAll(At): pass
        with self.assertRaises(Rejected):
            self.snap(set_segments(all_event(), raw, [UnexpectedAtAll('all')]), mode='all_messages')

    def test_normalized_component_must_have_raw_type_and_count(self):
        raw = [segment('face', payload=Opaque())]
        for normalized in ([Reply()], [Face(), Face()], [Plain('invented')], [object()]):
            with self.assertRaises(Rejected):
                self.snap(set_segments(Event(), raw, normalized))
        with self.assertRaises(Rejected):
            self.snap(set_segments(Event(), raw, [Face()]), components={})

    def test_raw_bot_at_is_still_required_and_only_top_level_counts(self):
        nested = segment('at', qq='100')
        for raw, normalized in [([segment('reply', chain=[nested])], [Reply()]),
                ([segment('node', content=[nested])], [Node()]),
                ([segment('text', text='@100 [CQ:at,qq=100]')], [Plain('@100 [CQ:at,qq=100]')]),
                ([segment('at', qq='all')], [At('all')]), ([segment('at', qq='999')], [At('999')])]:
            with self.assertRaisesRegex(Rejected, 'bot_mention_required'):
                self.snap(set_segments(group_event(), raw, normalized))
        value = self.snap(set_segments(group_event(), [nested, segment('reply', chain=Opaque())], [Reply()]))
        self.assertEqual(value['text'], '[引用消息]')

    def test_raw_sender_and_route_gates_are_unchanged_for_placeholders(self):
        for field, value in [('post_type', 'notice'), ('message_type', 'group'),
                ('self_id', 999), ('user_id', 999), ('sender', {'user_id': 999}),
                ('group_id', 300), ('message_id', 'other')]:
            event = set_segments(Event(), [segment('reply')], [Reply()])
            event.message_obj.raw_message[field] = value
            with self.assertRaises(Rejected): self.snap(event)
        for event in (Event(), group_event(), all_event()):
            set_segments(event, [segment('future_type')], [])
            event.sender = '999'
            with self.assertRaises(Rejected): self.snap(event, mode='all_messages')
        event = set_segments(all_event(sender='100'), [segment('future_type')], [])
        with self.assertRaises(Rejected): self.snap(event, mode='all_messages')

    def test_all_messages_accepts_nonowner_marker_without_at_only_in_allowed_group(self):
        event = set_segments(all_event(sender='201'), [segment('future_type')], [])
        self.assertEqual(self.snap(event, mode='all_messages')['sender_id'], '201')
        event.group = '400'; event.message_obj.raw_message['group_id'] = 400
        with self.assertRaises(Rejected): self.snap(event, mode='all_messages')

    def test_mixed_media_keep_original_segment_indices_and_sources(self):
        raw = [segment('reply', chain=Opaque()), segment('text', text=' inspect '),
               segment('image', url='https://media.example/image.png'), segment('face', id=3),
               segment('file', file_id='the-file', name='document.txt'),
               segment('record', url='https://must-not-fetch.example/record')]
        value = self.snap(set_segments(Event(), raw, [Reply(), Plain('inspect'), Image(), Face(), File(), Record()]))
        self.assertEqual([(item['segment_index'], item['kind']) for item in value['attachments']],
                         [(2, 'image'), (4, 'file')])
        self.assertEqual(value['attachments'][1]['file_id'], 'the-file')
        self.assertNotIn('must-not-fetch', json.dumps(value))

    def test_malformed_segments_and_bounded_output_still_rejected(self):
        for raw in [[], 'text', [None], [{'type': 'face'}], [segment('')],
                [segment('x' * 65)], [segment('bad\ntype')], [segment(True)],
                [segment('text', text=42)], [segment('markdown', content=42)],
                [segment('plain', text=None)], [segment('at', qq=True)],
                [segment('at', qq='-1')], [segment('at', qq='0')],
                [segment('at', qq='100\n')], [segment('future_type')] * 257,
                [segment('text', text='x' * 16384), segment('face')]]:
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(Rejected):
                self.snap(set_segments(Event(), raw, []))
        with self.assertRaisesRegex(Rejected, 'text_too_large'):
            self.snap(set_segments(Event(), [segment('markdown', content='x' * 16385)], [Plain('x' * 16385)]))

    def test_literal_cq_stays_literal_and_unknown_payload_is_not_text(self):
        text = '[CQ:reply,id=41] [CQ:at,qq=100]'
        value = self.snap(set_segments(Event(), [segment('text', text=text),
            segment('future_type', text='DO NOT COPY', payload=Opaque())], [Plain(text)]))
        self.assertEqual(value['text'], text + '[暂不支持的消息]')


class ChatSegmentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.module = load_main(self.temp.name)
        components = sys.modules['astrbot.api.message_components']
        components.ComponentTypes = COMPONENT_TYPES
        components.Image, components.File = Image, File
        self.config = {'enabled': True, 'platform_id': 'instance-1', 'adapter': 'aiocqhttp',
                       'self_id': '100', 'sender_id': '200', 'mcp_token': 'x' * 32,
                       'group_ids': ['300'], 'group_trigger_mode': 'all_messages'}
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def capture(self, event):
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        return next(reversed(self.plugin.captured))

    async def test_unsupported_captured_once_visible_in_history_and_replied_in_original_group(self):
        event = set_segments(all_event(), [segment('future_type', payload=Opaque())], [])
        first = await self.capture(event)
        self.assertEqual(self.plugin.host.store.read(first)['text'], '[暂不支持的消息]')
        duplicate = set_segments(all_event(), [segment('future_type', payload=Opaque())], [])
        self.assertEqual(await self.capture(duplicate), first)
        self.assertEqual(self.plugin.host.store.status()['messages'], 1)
        self.assertEqual((await self.plugin.host.reply(first, 'unsupported type received', 'one'))['state'],
                         'adapter_completed')
        self.assertEqual(event.sent, [])
        self.assertEqual(duplicate.sent[0][0].text, 'unsupported type received')
        next_event = all_event(text='next')
        next_event.message_obj.message_id = 'next'; next_event.message_obj.raw_message['message_id'] = 'next'
        second = await self.capture(next_event)
        history = self.plugin.host.store.history.page('group', '300')
        self.assertIn('[暂不支持的消息]', json.dumps(history, ensure_ascii=False))
        self.assertNotIn('conversation_context', self.plugin.host.store.read(second))

    async def test_reply_payload_never_read_and_route_mutation_still_blocks_send(self):
        event = set_segments(Event(), [segment('reply', chain=Opaque())], [Reply()])
        mid = await self.capture(event)
        event.message_obj.raw_message['user_id'] = 999
        self.assertEqual((await self.plugin.host.reply(mid, 'reply', 'one'))['state'], 'not_sent')
        self.assertEqual(event.sent, [])

    async def test_narrowed_mode_or_removed_group_blocks_pending_placeholder_reply(self):
        for key, value in [('group_trigger_mode', 'owner_at'), ('group_ids', [])]:
            self.config.update(group_trigger_mode='all_messages', group_ids=['300'])
            event = set_segments(all_event(), [segment('face')], [Face()])
            event.message_obj.message_id = key; event.message_obj.raw_message['message_id'] = key
            mid = await self.capture(event)
            self.config[key] = value
            self.assertEqual((await self.plugin.host.reply(mid, 'reply', key))['state'], 'not_sent')
            self.assertEqual(event.sent, [])

    async def test_missing_mapping_keeps_plain_compatibility_and_rejects_unmapped_components(self):
        await self.plugin.terminate()
        del sys.modules['astrbot.api.message_components'].ComponentTypes
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertEqual(self.plugin.component_types, {})
        await self.capture(Event())
        event = set_segments(Event(), [segment('reply')], [Reply()])
        await self.plugin._on_message(event)
        self.assertFalse(event.stopped)

    async def test_disabled_plugin_does_not_inspect_invalid_optional_mapping(self):
        await self.plugin.terminate()
        sys.modules['astrbot.api.message_components'].ComponentTypes = Opaque()
        self.config['enabled'] = False
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.assertFalse(self.plugin.ready)


if __name__ == '__main__': unittest.main()
