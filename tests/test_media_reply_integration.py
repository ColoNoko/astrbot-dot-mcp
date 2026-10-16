import asyncio,base64,importlib.util,json,types,unittest
from pathlib import Path
from unittest.mock import AsyncMock
import aiohttp
import test_main as main_fixtures
from test_main import Event
from test_groups import group_event

class MediaReplyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=main_fixtures.MainTests.asyncSetUp
    asyncTearDown=main_fixtures.MainTests.asyncTearDown
    async def capture(self,group=False):
        event=group_event() if group else Event()
        if group:
            self.config.update(group_ids=['300'],group_trigger_mode='all_messages')
        event.get_self_id=lambda:'100'
        event.bot=types.SimpleNamespace(call_action=AsyncMock(return_value={'message_id':123}))
        await self.plugin._on_message(event)
        self.assertTrue(event.stopped)
        return next(reversed(self.plugin.captured)),event
    async def send(self,mid,key='media',data=b'print(1)',kind='file',name='example.py'):
        return await self.plugin._reply_attachment(mid,kind,name,base64.b64encode(data).decode(),key)
    async def test_private_file_dedupe_conflict_and_metadata_only_storage(self):
        mid,event=await self.capture();result=await self.send(mid)
        self.assertEqual(result['state'],'adapter_completed');await self.send(mid)
        event.bot.call_action.assert_awaited_once()
        args=event.bot.call_action.call_args.kwargs
        self.assertEqual((args['action'],args['user_id'],args['self_id']),('send_private_msg',200,100))
        self.assertEqual(base64.b64decode(args['message'][0]['data']['file'][9:]),b'print(1)')
        with self.assertRaisesRegex(Exception,'reply_key_conflict'):await self.send(mid,name='other.py')
        stored=self.plugin.host.store.db.execute('SELECT text FROM replies').fetchone()[0]
        self.assertNotIn('cHJpbnQoMSk=',stored);self.assertIn('sha256',stored)
        self.plugin.captured.clear();self.plugin.captured_bots.clear()
        self.assertEqual((await self.send(mid))['state'],'adapter_completed')
        self.assertEqual((await self.send(mid,key='fresh'))['state'],'not_sent')
    async def test_group_route_and_revocation(self):
        mid,event=await self.capture(group=True);await self.send(mid)
        self.assertEqual(event.bot.call_action.call_args.kwargs['group_id'],300)
        self.config['group_ids']=[]
        self.assertEqual((await self.send(mid,key='revoked'))['state'],'not_sent')
        event.bot.call_action.assert_awaited_once()
    async def test_replaced_bot_never_called_and_unknown_ack_uncertain(self):
        mid,event=await self.capture();original=event.bot
        event.bot=types.SimpleNamespace(call_action=AsyncMock(return_value={'message_id':9}))
        self.assertEqual((await self.send(mid))['state'],'not_sent');event.bot.call_action.assert_not_called()
        event.bot=original;original.call_action.return_value=None
        self.assertEqual((await self.send(mid,key='uncertain'))['state'],'uncertain')
        await self.send(mid,key='uncertain');original.call_action.assert_awaited_once()
    async def test_capacity_failure_stops_without_send_and_persists_safe_alert(self):
        ingest=self.plugin.host.store.ingest
        def fail(*args,**kwargs):raise self.module.Rejected('storage_full')
        self.plugin.host.store.ingest=fail
        event=Event();await self.plugin._on_message(event)
        self.assertTrue(event.stopped);self.assertEqual(event.sent,[]);self.assertEqual(self.plugin.captured,{})
        status=self.plugin.capacity_alert.status();self.assertTrue(status['active']);self.assertEqual(status['count'],1)
        self.assertTrue(status['persistent']);self.assertNotIn('hello',json.dumps(status))
        self.plugin.host.store.ingest=ingest;await self.plugin._on_message(Event())
        self.assertFalse(self.plugin.capacity_alert.status()['active'])
    async def test_real_rpc_large_media_only_and_seven_tool_discovery(self):
        mid,event=await self.capture()
        spec=importlib.util.spec_from_file_location('plugin_mock.media_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        ep=importlib.util.module_from_spec(spec);spec.loader.exec_module(ep)
        self.assertIn('qq_reply_attachment',[tool['name'] for tool in ep.TOOLS])
        endpoint=ep.Endpoint(self.plugin.host,'x'*32,['callbacks.example.com'],port=0);await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                url=f'http://127.0.0.1:{endpoint.port}/mcp'
                headers={'Authorization':'Bearer '+'x'*32,'MCP-Protocol-Version':ep.PROTOCOL_VERSION,'Mcp-Method':'tools/call'}
                body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_reply_attachment','arguments':{'message_id':mid,'reply_key':'rpc','kind':'file','filename':'x.bin','data_base64':base64.b64encode(b'x'*100000).decode()}}}
                async with client.post(url,headers=headers,json=body) as response:
                    result=await response.json();self.assertEqual(response.status,200)
                self.assertEqual(result['result']['structuredContent']['state'],'adapter_completed')
                body['params']={'name':'qq_reply','arguments':{'message_id':mid,'reply_key':'bad','text':'x'*100000}}
                async with client.post(url,headers=headers,json=body) as response:self.assertEqual(response.status,413)
                self.assertEqual(endpoint._media_requests._value,2)
        finally:await endpoint.close()

    async def test_duplicate_does_not_clear_capacity_alarm(self):
        mid,event=await self.capture()
        self.plugin.capacity_alert.note_rejection('storage_full')
        self.plugin.runtime_error='storage_full'
        await self.plugin._on_message(event)
        self.assertTrue(self.plugin.capacity_alert.status()['active'])
        self.assertEqual(self.plugin.runtime_error,'storage_full')

    async def test_media_history_is_labeled_and_never_contains_base64(self):
        mid,event=await self.capture();await self.send(mid)
        newer=Event();newer.message_obj.message_id='newer'
        newer.message_obj.raw_message['message_id']='newer'
        await self.plugin._on_message(newer)
        current=next(reversed(self.plugin.captured))
        history=self.plugin.host.store.history.page('private')
        encoded=json.dumps(history,ensure_ascii=False)
        self.assertNotIn('conversation_context',self.plugin.host.store.read(current))
        self.assertIn('附件回复',encoded);self.assertNotIn('dot_media_v1',encoded)
        self.assertNotIn('cHJpbnQoMSk=',encoded)
