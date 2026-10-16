"""Policy changes revoke captured and cached attachment access immediately."""
import asyncio
import unittest

import test_attachment_resolution as fixtures
from test_access_policy import rule


class PolicyAttachmentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = fixtures.AttachmentResolutionTests.asyncTearDown
    event = fixtures.AttachmentResolutionTests.event
    capture = fixtures.AttachmentResolutionTests.capture

    def secondary_event(self, *, group=False):
        self.config['private_sender_ids'] = ['200', '201']
        event = self.event(group=group)
        event.sender = '201'
        event.message_obj.raw_message['user_id'] = 201
        event.message_obj.raw_message['sender']['user_id'] = 201
        return event

    async def test_secondary_private_attachment_has_honest_sender_scope(self):
        mid, aid = await self.capture(self.secondary_event())
        data = await self.plugin._read_attachment(mid, aid)
        self.assertEqual(data['structuredContent']['sender_id'], '201')
        self.assertEqual(data['structuredContent']['privacy_scope'], 'private_sender_only')
        self.assertEqual(data['structuredContent']['message_id'], mid)

    async def test_private_removed_before_first_download_never_calls_bot_or_transport(self):
        event = self.secondary_event()
        mid, aid = await self.capture(event)
        self.config['private_sender_ids'] = ['200']
        with self.assertRaises(Exception):
            await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_private_removed_after_cache_fill_cannot_read_cached_bytes(self):
        mid, aid = await self.capture(self.secondary_event())
        await self.plugin._read_attachment(mid, aid)
        calls = list(self.transport.calls)
        self.config['private_sender_ids'] = ['200']
        with self.assertRaises(Exception):
            await self.plugin._read_attachment(mid, aid)
        self.assertEqual(self.transport.calls, calls)

    async def test_private_removed_during_download_returns_no_bytes(self):
        mid, aid = await self.capture(self.secondary_event())
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.plugin._read_attachment(mid, aid))
        await self.transport.started.wait()
        self.config['private_sender_ids'] = ['200']
        self.transport.block.set()
        with self.assertRaises(Exception):
            await task

    async def test_group_selected_sender_removed_revokes_attachment(self):
        self.config['group_rules'] = [rule('300', 'selected', senders=['201'])]
        event = self.secondary_event(group=True)
        mid, aid = await self.capture(event)
        self.config['group_rules'] = [rule('300', 'selected', senders=['202'])]
        with self.assertRaises(Exception):
            await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_group_all_to_at_revalidates_original_raw_trigger_for_download(self):
        self.config['group_rules'] = [rule('300', 'all')]
        event = self.secondary_event(group=True)
        mid, aid = await self.capture(event)
        self.config['group_rules'] = [rule('300', 'at')]
        with self.assertRaisesRegex(Exception, '^bot_mention_required$'):
            await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])

    async def test_mutated_private_sender_cannot_reuse_attachment_provenance(self):
        event = self.secondary_event()
        mid, aid = await self.capture(event)
        event.sender = '200'
        event.message_obj.raw_message['user_id'] = 200
        event.message_obj.raw_message['sender']['user_id'] = 200
        with self.assertRaisesRegex(Exception, '^message_identity_changed$'):
            await self.plugin._read_attachment(mid, aid)
        event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls, [])


if __name__ == '__main__':
    unittest.main()
