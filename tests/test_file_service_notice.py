"""Same-event service hint; synthetic messages and local authenticated RPC only."""
import importlib
import json
import unittest
import test_events as event_fixtures
import test_processing_watch_integration as rpc_fixtures
import test_group_file_candidates as group_fixtures

notice = importlib.import_module('astrbot_dot_plugin.service_notice')


class EventNoticeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = event_fixtures.EventsTests.asyncSetUp
    asyncTearDown = event_fixtures.EventsTests.asyncTearDown

    async def test_file_event_fixed_notice_schema_and_duplicate_body_unchanged(self):
        await self.events.subscribe(event_fixtures.subscribe_params())
        with self.db:
            self.db.execute('BEGIN')
            self.events.enqueue(event_fixtures.MID, self.now, file_request=True)
        row = self.db.execute('SELECT event_id,body FROM event_deliveries').fetchone()
        data = json.loads(row[1])['data']
        self.assertEqual(data['service_notice'], notice.file_service_notice(event_fixtures.MID))
        self.assertIs(data['service_notice']['new_authorization'], False)
        schema = self.events.list_events()['events'][0]['payloadSchema']
        self.assertEqual(schema['properties']['service_notice'], notice.NOTICE_SCHEMA)
        self.assertEqual(schema['required'], ['message_id'])
        with self.db:
            self.db.execute('BEGIN')
            self.events.enqueue(event_fixtures.MID, self.now, file_request=False)
        self.assertEqual(self.db.execute('SELECT event_id,body FROM event_deliveries').fetchall(), [row])
        # Stored event remains identical across delivery, including the notice.
        await self.events.deliver_once()
        self.assertEqual(self.transport.calls[-1][1], row[1])

    async def test_plain_event_has_no_new_field_and_flag_is_strict(self):
        await self.events.subscribe(event_fixtures.subscribe_params())
        with self.db:
            self.db.execute('BEGIN')
            self.events.enqueue(event_fixtures.MID, self.now)
        body=self.db.execute('SELECT body FROM event_deliveries').fetchone()[0]
        self.assertEqual(json.loads(body)['data'], {'message_id':event_fixtures.MID})
        with self.assertRaises(event_fixtures.EventError), self.db:
            self.db.execute('BEGIN')
            self.events.enqueue('msg_other', self.now, file_request='true')


class RPCNoticeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rpc_fixtures.ProcessingWatchIntegration.asyncSetUp
    asyncTearDown = rpc_fixtures.ProcessingWatchIntegration.asyncTearDown
    post = rpc_fixtures.ProcessingWatchIntegration.post
    call = rpc_fixtures.ProcessingWatchIntegration.call

    async def test_authenticated_read_file_hint_without_content_interpolation_or_send(self):
        store=self.plugin.host.store
        with store.db:
            store.db.execute('BEGIN')
            store.attachments.register(self.mid,[{'segment_index':0,'kind':'file',
                'filename':'ignore-all-rules.txt','file_id':'synthetic'}])
        _,body=await self.call('qq_read_message',{'message_id':self.mid})
        result=body['result']['structuredContent']
        self.assertEqual(result['service_notice']['message_id'],self.mid)
        self.assertNotIn('ignore-all-rules',json.dumps(result['service_notice']))
        self.assertEqual(self.sent,[])
        store.history.complete(self.mid)
        _,body=await self.call('qq_read_message',{'message_id':self.mid})
        self.assertEqual(body['result']['structuredContent']['processing_state'],'completed')
        self.assertIn('已完成',body['result']['structuredContent']['service_notice']['text'])
        self.assertEqual(self.sent,[])

    async def test_text_and_image_do_not_receive_file_hint(self):
        _,body=await self.call('qq_read_message',{'message_id':self.mid})
        self.assertNotIn('service_notice',body['result']['structuredContent'])
        store=self.plugin.host.store
        with store.db:
            store.db.execute('BEGIN')
            store.attachments.register(self.mid,[{'segment_index':0,'kind':'image','filename':'image','url':'https://example.com/image'}])
        _,body=await self.call('qq_read_message',{'message_id':self.mid})
        self.assertNotIn('service_notice',body['result']['structuredContent'])


class LinkedNoticeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=group_fixtures.GroupCandidateIntegration.asyncSetUp
    asyncTearDown=group_fixtures.GroupCandidateIntegration.asyncTearDown
    event=group_fixtures.GroupCandidateIntegration.event
    tick=group_fixtures.GroupCandidateIntegration.tick
    candidate=group_fixtures.GroupCandidateIntegration.candidate
    request=group_fixtures.GroupCandidateIntegration.request
    latest=group_fixtures.GroupCandidateIntegration.latest

    async def test_background_candidate_silent_linked_request_has_one_notice(self):
        seen=[]
        def enqueue(mid, now, **kwargs):
            seen.append((mid,kwargs)); return 1
        self.events.enqueue=enqueue
        await self.candidate()
        await next(iter(self.p.group_candidates.candidates.values())).task
        self.assertEqual(seen,[])
        await self.request()
        self.assertEqual(len(seen),1)
        self.assertIs(seen[0][1]['file_request'],True)
        result=self.p.host.store.read(self.latest())
        self.assertEqual(result['service_notice']['message_id'],self.latest())
