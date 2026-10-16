"""Native API-shaped media/history integration; synthetic files, no QQ or network."""
import asyncio
import json
import tempfile
import types
import unittest
from test_main import Event, Plain, load_main
from test_media_snapshot import Image, File
from test_attachments import FakeTransport, PNG
from test_groups import group_event

class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.module=load_main(self.temp.name)
        self.config={'enabled':True,'platform_id':'instance-1','adapter':'aiocqhttp','self_id':'100',
                     'sender_id':'200','group_ids':['300'],'group_trigger_mode':'all_messages','mcp_token':'x'*32}
        self.p=self.module.Main(object(),self.config);await self.p.initialize()
        self.p.media_types={'image':Image,'file':File}
        self.transport=FakeTransport();await self.p.host.store.attachments.transport.close()
        self.p.host.store.attachments.transport=self.transport
        self.req=types.SimpleNamespace(username='admin',plugin_name='astrbot_plugin_dot_private')
    async def asyncTearDown(self):
        await self.p.terminate();self.temp.cleanup()
    async def capture(self,event):
        await self.p._on_message(event);self.assertTrue(event.stopped)
        return next(reversed(self.p.captured))
    def image(self):
        e=Event();e.message_obj.raw_message['message']=[{'type':'image','data':{'url':'https://media.example.com/test.png'}}]
        e.messages=[Image()];return e
    async def test_image_read_and_private_history_are_scoped(self):
        mid=await self.capture(self.image());value=self.p.host.store.read(mid)
        aid=value['attachments'][0]['attachment_id'];result=await self.p._read_attachment(mid,aid)
        self.assertTrue(any(item['type']=='image' for item in result['content']))
        self.assertEqual(value['processing_state'],'pending')
        csrf=self.p.admin.status(self.req)['csrf_token']
        history=self.p.admin.history(self.req,{'csrf_token':csrf,'action':'messages','conversation_type':'private','group_id':'','before':None,'limit':30})
        self.assertEqual(history['messages'][0]['message_id'],mid)
        self.assertNotIn('https://',json.dumps(history))
        self.assertEqual(history['messages'][0]['processing_state'],'pending')
        self.assertEqual(history['storage']['limit_bytes'],10*1024*1024)
    async def test_exact_group_file_segment_resolver_and_same_group_reply(self):
        e=group_event();e.messages=[Plain('inspect'),File()]
        e.message_obj.raw_message['message']=[{'type':'text','data':{'text':'inspect'}},{'type':'file','data':{'file_id':'right-id','name':'x.png'}}]
        calls=[]
        async def action(**kwargs):calls.append(kwargs);return {'url':'https://media.example.com/f'}
        e.get_self_id=lambda:'100';e.bot=types.SimpleNamespace(call_action=action)
        mid=await self.capture(e);aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        await self.p._read_attachment(mid,aid)
        self.assertEqual(calls,[{'action':'get_group_file_url','file_id':'right-id','group_id':300,'self_id':100}])
        self.assertEqual((await self.p.host.reply(mid,'looked','k'))['state'],'adapter_completed')
        self.assertEqual(self.p.host.store.read(mid)['processing_state'],'completed')
    async def test_route_change_while_file_resolver_waits_prevents_download(self):
        e=self.image();e.messages=[File()];e.message_obj.raw_message['message']=[{'type':'file','data':{'file_id':'f'}}]
        entered,release=asyncio.Event(),asyncio.Event()
        async def action(**kwargs):entered.set();await release.wait();return {'url':'https://media.example.com/f'}
        e.get_self_id=lambda:'100';e.bot=types.SimpleNamespace(call_action=action);mid=await self.capture(e)
        aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        task=asyncio.create_task(self.p._read_attachment(mid,aid));await entered.wait()
        self.config['enabled']=False;release.set()
        with self.assertRaises(Exception):await task
        self.assertEqual(self.transport.calls,[])
    async def test_media_mutation_unknown_id_and_removed_group_rejected(self):
        e=self.image();mid=await self.capture(e);aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        with self.assertRaises(Exception):await self.p._read_attachment(mid,'att_'+'0'*32)
        e.message_obj.raw_message['message'][0]['data']['url']='https://different.example/f'
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)
        self.assertEqual(self.transport.calls,[])
    async def test_pending_storage_failure_visible_without_llm_fallback(self):
        def full(*args,**kwargs):raise self.module.Rejected('storage_full')
        self.p.host.store.history.admit=full
        e=Event();await self.p._on_message(e)
        self.assertTrue(e.stopped);self.assertEqual(self.p.runtime_error,'storage_full')
        self.assertEqual(self.p.host.store.status()['messages'],0)
    async def test_history_auth_and_read_do_not_complete(self):
        mid=await self.capture(Event())
        with self.assertRaisesRegex(Exception,'forbidden'):
            self.p.admin.history(self.req,{'csrf_token':'bad','action':'conversations','before':None,'limit':50})
        self.p.host.store.read(mid)
        self.assertEqual(self.p.host.store.history.status()['pending_messages'],1)
        self.p.host.store.history.complete(mid)
        self.assertEqual(self.p.host.store.history.status()['pending_messages'],0)

    async def test_same_source_changed_media_is_collision_and_restart_keeps_metadata(self):
        e=self.image();mid=await self.capture(e)
        value=self.p.host.store.read(mid);aid=value['attachments'][0]['attachment_id']
        e.message_obj.raw_message['message'][0]['data']['url']='https://changed.example/f'
        await self.p._on_message(e)
        self.assertEqual(self.p.host.store.status()['messages'],1)
        await self.p._stop_runtime();await self.p._start_enabled()
        self.assertEqual(self.p.host.store.read(mid)['attachments'][0]['attachment_id'],aid)
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)
        self.assertEqual(self.transport.calls,[])

    async def test_distinct_reply_keys_not_limited_by_message_quota_and_retries_idempotent(self):
        mid=await self.capture(Event())
        for number in range(12):
            self.assertEqual((await self.p.host.reply(mid,'reply',str(number)))['state'],'adapter_completed')
        with self.assertRaisesRegex(Exception,'reply_key_conflict'):
            await self.p.host.reply(mid,'changed','0')
        self.assertEqual((await self.p.host.reply(mid,'reply','0'))['state'],'adapter_completed')

    async def test_uncertain_reply_stays_protected_until_later_explicit_completion(self):
        mid=await self.capture(Event());store=self.p.host.store
        store.claim(mid,'first','first');store.finish('first','uncertain')
        store.claim(mid,'second','second');store.finish('second','adapter_completed')
        self.assertEqual(store.history.prune(),0)
        self.assertEqual(store.history.pending('private')['messages'][0]['message_id'],mid)
        store.history._ensure_schema()
        self.assertEqual(store.read(mid)['processing_state'],'pending')
        store.history.complete(mid)
        self.assertEqual(store.history.prune(),1)
        self.assertEqual(store.claim(mid,'first','first'),('uncertain',False))

    async def test_new_send_revokes_old_explicit_completion(self):
        mid=await self.capture(Event());store=self.p.host.store;store.history.complete(mid)
        store.claim(mid,'first','first');store.finish('first','uncertain')
        self.assertEqual(store.read(mid)['processing_state'],'pending')
        self.assertEqual(store.history.prune(),0)

    async def test_pending_listing_scope_pagination_and_no_implicit_ack(self):
        private=await self.capture(Event())
        group=group_event();group.message_obj.message_id='group1';group.message_obj.raw_message['message_id']='group1'
        group_id=await self.capture(group)
        result=self.p.host.store.history.pending('private','',limit=1)
        self.assertEqual([v['message_id'] for v in result['messages']],[private])
        result=self.p.host.store.history.pending('group','300',limit=1)
        self.assertEqual([v['message_id'] for v in result['messages']],[group_id])
        self.assertEqual(self.p.host.store.history.status()['pending_messages'],2)
        for kind,gid in [('private','300'),('group',''),('any','')]:
            with self.assertRaises(Exception):self.p.host.store.history.pending(kind,gid)
