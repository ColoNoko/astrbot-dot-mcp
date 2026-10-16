"""Real authenticated local RPC wiring; no QQ or external callbacks."""
import json
import unittest
from pathlib import Path
import test_media_request_limits as fixtures


class ProcessingWatchIntegration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MediaRequestLimitTests.asyncSetUp
    asyncTearDown = fixtures.MediaRequestLimitTests.asyncTearDown
    post = fixtures.MediaRequestLimitTests.post

    async def call(self, name, arguments):
        return await self.post({'jsonrpc':'2.0','id':1,'method':'tools/call',
                               'params':{'name':name,'arguments':arguments}})

    async def test_default_off_capture_and_successful_read_are_durable(self):
        watch=self.plugin.host.store.processing_watch
        before=watch.get(self.mid)
        self.assertFalse(before['eligible_at_capture'])
        self.assertIsNone(before['first_read_at'])
        self.plugin.config['experimental_unread_reminders']=True
        self.assertFalse(watch.get(self.mid)['eligible_at_capture'])
        _,body=await self.call('qq_read_message',{'message_id':self.mid})
        self.assertFalse(body['result']['isError'])
        result=body['result']['structuredContent']['processing_observation']
        self.assertIsNotNone(result['processing_started_at'])
        self.assertIsNotNone(result['first_read_at'])
        self.assertEqual(result['stopped_reason'],'read')
        self.assertEqual(self.sent,[])

    async def test_current_auth_denial_never_marks_read_or_attachment_started(self):
        self.plugin.config['enabled']=False
        for tool,args in [('qq_read_message',{'message_id':self.mid}),
                          ('qq_read_attachment',{'message_id':self.mid,'attachment_id':'att_'+'a'*32})]:
            _,body=await self.call(tool,args)
            self.assertTrue(body['result']['isError'])
        value=self.plugin.host.store.processing_watch.get(self.mid)
        self.assertIsNone(value['first_read_at'])
        self.assertIsNone(value['attachment_started_at'])

    async def test_attachment_entry_stops_even_when_attachment_id_fails(self):
        _,body=await self.call('qq_read_attachment',{'message_id':self.mid,'attachment_id':'att_'+'a'*32})
        self.assertTrue(body['result']['isError'])
        value=self.plugin.host.store.processing_watch.get(self.mid)
        self.assertIsNotNone(value['attachment_started_at'])
        self.assertIsNone(value['first_read_at'])
        self.assertEqual(self.sent,[])

    async def test_status_and_target_trace_expose_same_persistent_observation(self):
        self.plugin.config['remote_diagnostics_enabled']=True
        _,body=await self.call('qq_status',{})
        self.assertIn('processing_watch',body['result']['structuredContent'])
        _,body=await self.call('qq_reply',{'message_id':self.mid,'reply_key':'trace',
            'text':'DOT_DIAG_V1\n'+json.dumps({'command':'message_trace','message_id':self.mid,'limit':1})})
        self.assertIn('processing_observation',body['result']['structuredContent'])
        self.assertEqual(self.sent,[])
        self.assertEqual(self.plugin.host.store.status()['replies'],{})

    async def test_config_strict_false_default_and_capture_after_enable(self):
        schema=json.loads((Path(__file__).resolve().parents[1]/'_conf_schema.json').read_text())
        self.assertIs(schema['experimental_unread_reminders']['default'],False)
        for flag in (None,False,1,'true'):
            self.plugin.config['experimental_unread_reminders']=flag
            self.assertFalse(self.endpoint._reminders_enabled())
        self.plugin.config['experimental_unread_reminders']=True
        self.assertTrue(self.endpoint._reminders_enabled())
        from test_main import Event
        event=Event();event.message_obj.message_id='second-new-message'
        event.message_obj.raw_message['message_id']='second-new-message'
        await self.plugin._on_message(event)
        mid=next(reversed(self.plugin.captured))
        self.assertTrue(self.plugin.host.store.processing_watch.get(mid)['eligible_at_capture'])
