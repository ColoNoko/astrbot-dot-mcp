"""API-shaped mocks only: these do not prove a real AstrBot plugin load."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from test_host import Event as BaseEvent, Plain
try:
    from aiocqhttp import Event as CQEvent
    REAL_CQ_EVENT = True
except ImportError:
    class CQEvent(dict):
        pass
    REAL_CQ_EVENT = False
class Event(BaseEvent):
    def __init__(self):
        super().__init__(); self.adapter='aiocqhttp'
        self.message_obj.raw_message=CQEvent({'post_type':'message','message_type':'private','user_id':200,'self_id':100, 'sender':{'user_id':200}, 'message_id':'original-1', 'message':[{'type':'text','data':{'text':'hello'}}]})

class At:
    def __init__(self, qq): self.qq=qq

ROOT=Path(__file__).resolve().parents[1]
logs=[]
class Star:
    def __init__(self,context): self.context=context
class Filter:
    EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE='private', GROUP_MESSAGE='group')
    @staticmethod
    def event_message_type(*args,**kwargs): return lambda f:f
    @staticmethod
    def on_astrbot_loaded(*args,**kwargs): return lambda f:f
class Endpoint:
    def __init__(self,*args,**kwargs): pass
    async def start(self): pass
    async def close(self): pass

def load_main(root):
    modules={
      'astrbot':types.ModuleType('astrbot'), 'astrbot.api':types.ModuleType('astrbot.api'),
      'astrbot.api.event':types.ModuleType('astrbot.api.event'),
      'astrbot.api.star':types.ModuleType('astrbot.api.star'),
      'astrbot.api.message_components':types.ModuleType('astrbot.api.message_components'),
      'aiocqhttp':types.ModuleType('aiocqhttp'),
      'plugin_mock':types.ModuleType('plugin_mock'), 'plugin_mock.endpoint':types.ModuleType('plugin_mock.endpoint')}
    modules['astrbot.api'].AstrBotConfig=dict
    modules['astrbot.api'].logger=types.SimpleNamespace(error=logs.append)
    modules['astrbot.api.event'].filter=Filter
    modules['astrbot.api.event'].AstrMessageEvent=Event
    modules['astrbot.api.star'].Star=Star; modules['astrbot.api.star'].Context=object
    modules['astrbot.api.message_components'].Plain=Plain
    modules['astrbot.api.message_components'].At=At
    modules['astrbot.api.star'].StarTools=types.SimpleNamespace(get_data_dir=lambda name:Path(root)/'plugin_data'/name)
    modules['aiocqhttp'].Event=CQEvent
    modules['plugin_mock'].__path__=[str(ROOT)]
    modules['plugin_mock.endpoint'].Endpoint=Endpoint
    sys.modules.update(modules)
    spec=importlib.util.spec_from_file_location('plugin_mock.main',ROOT/'main.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

class MainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.module=load_main(self.tmp.name)
        self.config={'enabled':True,'platform_id':'instance-1','adapter':'aiocqhttp',
                     'self_id':'100','sender_id':'200','mcp_token':'x'*32}
        self.plugin=self.module.Main(object(),self.config)
        await self.plugin.initialize()
    async def asyncTearDown(self):
        await self.plugin.terminate(); self.tmp.cleanup()
    async def test_stop_only_matching_then_plain_reply(self):
        for attr,value in [('sender','999'),('group','g'),('adapter','wrong'),('private',False),('messages',[object()])]:
            e=Event(); setattr(e,attr,value)
            await self.plugin.on_private_message(e)
            self.assertFalse(e.stopped)
        e=Event(); await self.plugin.on_private_message(e); self.assertTrue(e.stopped)
        mid=next(iter(self.plugin.captured))
        result=await self.plugin.host.reply(mid,'[CQ:image,file=x]','key')
        self.assertEqual(result['state'],'adapter_completed')
        self.assertEqual(e.sent[0][0].text,'[CQ:image,file=x]')
    async def test_raw_route_mismatch_never_captured(self):
        for key,value in [('self_id',999),('user_id',999),('message_type','group'),('group_id',123),('user_id',True)]:
            e=Event(); e.message_obj.raw_message[key]=value
            await self.plugin.on_private_message(e)
            self.assertFalse(e.stopped)
        self.assertEqual(self.plugin.captured,{})
    async def test_mutated_raw_route_never_sent(self):
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured)); e.message_obj.raw_message['self_id']=999
        self.assertEqual((await self.plugin.host.reply(mid,'x','key'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_plain_dict_raw_event_rejected_before_capture_and_send(self):
        e=Event(); e.message_obj.raw_message=dict(e.message_obj.raw_message)
        await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured)); e.message_obj.raw_message=dict(e.message_obj.raw_message)
        self.assertEqual((await self.plugin.host.reply(mid,'x','key'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_malformed_raw_arrays_and_normalization_mismatch_rejected(self):
        for raw in ('hello', [], [{'data':{}}], [{'type':'face','data':None}],
                    [{'type':'markdown','data':{'content':'different'}}],
                    [{'type':'text','data':{'text':42}}], [{'type':'file','data':{}}]):
            e=Event(); e.message_obj.raw_message['message']=raw
            await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
        self.assertEqual(self.plugin.captured,{})
    async def test_raw_source_id_must_match(self):
        for value in ('other', None, True):
            e=Event(); e.message_obj.raw_message['message_id']=value
            await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
    async def test_raw_spaces_preserved_and_cq_literal(self):
        e=Event(); text='  [CQ:at,qq=all] hello  '
        e.messages=[Plain(text.strip())]
        e.message_obj.raw_message['message']=[{'type':'text','data':{'text':text[:8]}},
                                              {'type':'text','data':{'text':text[8:]}}]
        await self.plugin.on_private_message(e); self.assertTrue(e.stopped)
        mid=next(iter(self.plugin.captured))
        self.assertEqual(self.plugin.host.store.read(mid)['text'],text)
    async def test_normalization_drift_rejected(self):
        e=Event(); e.messages=[Plain('changed')]
        await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
    async def test_missing_public_hook_fails_closed(self):
        await self.plugin.terminate()
        self.module.AstrMessageEvent=type('MissingHooks',(),{})
        await self.plugin.initialize()
        self.assertEqual(self.plugin.runtime_error, 'startup_failed')
        self.assertFalse(self.plugin.ready); self.assertIsNone(self.plugin.host)
    async def test_runtime_missing_send_hook_rejected(self):
        e=Event(); e.send=None
        await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
    async def test_disable_after_awaited_preflight_prevents_send(self):
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured)); entered=asyncio.Event(); release=asyncio.Event()
        original=self.plugin.host.preflight
        async def preflight(row):
            result=await original(row); entered.set(); await release.wait(); return result
        self.plugin.host.preflight=preflight
        task=asyncio.create_task(self.plugin.host.reply(mid,'x','key'))
        await entered.wait(); self.config['enabled']=False; release.set()
        self.assertEqual((await task)['state'],'not_sent'); self.assertEqual(e.sent,[])
    async def test_stop_before_ingest_even_database_failure(self):
        e=Event()
        def fail(value):
            self.assertTrue(e.stopped); raise RuntimeError('private token')
        self.plugin.host.store.ingest=fail
        await self.plugin.on_private_message(e)
        self.assertEqual(logs[-1],'dot_private_ingest_failed message_rejected; free_completed_history_then_resend')
    async def test_restart_read_survives_old_reply_not_sent(self):
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured))
        await self.plugin.terminate(); await self.plugin.initialize()
        self.assertEqual(self.plugin.host.store.read(mid)['text'],'hello')
        self.assertEqual((await self.plugin.host.reply(mid,'x','key'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_config_change_rechecked_before_send(self):
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured)); self.config['sender_id']='999'
        self.assertEqual((await self.plugin.host.reply(mid,'x','key'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_mutated_captured_identity_not_sent(self):
        e=Event(); await self.plugin.on_private_message(e)
        mid=next(iter(self.plugin.captured)); e.unified_msg_origin='changed'
        self.assertEqual((await self.plugin.host.reply(mid,'x','key'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_startup_error_redacted(self):
        await self.plugin.terminate()
        self.config['platform_id']='bad\nsecret-token'
        await self.plugin.initialize()
        self.assertEqual(self.plugin.runtime_error, 'startup_failed')
        self.assertNotIn('secret-token',logs[-1])
    async def test_cleanup_continues_after_endpoint_failure(self):
        host=self.plugin.host
        async def fail(): raise RuntimeError('token-secret')
        self.plugin.endpoint.close=fail
        await self.plugin.terminate()
        self.assertTrue(host.stopping)
        self.assertIsNone(self.plugin.host)
        with self.assertRaises(Exception): host.store.status()
        self.assertNotIn('token-secret',logs[-1])
    async def test_cancelled_initialize_cleans_resources(self):
        await self.plugin.terminate()
        entered=asyncio.Event()
        original=self.module.Endpoint
        class Blocking(Endpoint):
            async def start(self): entered.set(); await asyncio.sleep(10)
        self.module.Endpoint=Blocking
        task=asyncio.create_task(self.plugin.initialize())
        await entered.wait(); host=self.plugin.host; task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertIsNone(self.plugin.host)
        with self.assertRaises(Exception): host.store.status()
        self.module.Endpoint=original
    async def test_non_qq_adapter_rejected(self):
        await self.plugin.terminate(); self.config['adapter']='telegram'
        await self.plugin.initialize()
        self.assertEqual(self.plugin.runtime_error, 'startup_failed')
    async def test_disabled_by_default(self):
        await self.plugin.terminate(); self.config.pop('enabled')
        await self.plugin.initialize()
        e=Event(); await self.plugin.on_private_message(e)
        self.assertFalse(e.stopped); self.assertIsNone(self.plugin.host)


class DiscoveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.module=load_main(self.tmp.name)
        self.instances=[]
        class Config(dict):
            saves=0
            def save_config(self): self.saves+=1
        self.config=Config(enabled=False,platform_id='',adapter='',self_id='',sender_id='',mcp_token='',group_ids=[])
        self.context=types.SimpleNamespace(platform_manager=types.SimpleNamespace(get_insts=lambda:self.instances),
            get_config=lambda:{'platform':[{'id':i.meta().id,'type':'aiocqhttp','enable':True} for i in self.instances]},
            get_platform_inst=lambda wanted:next((i for i in self.instances if i.meta().id==wanted),None))
        self.plugin=self.module.Main(self.context,self.config)
    async def asyncTearDown(self):
        await self.plugin.terminate(); self.tmp.cleanup()
    def instance(self,name):
        return types.SimpleNamespace(meta=lambda:types.SimpleNamespace(name='aiocqhttp',id=name))
    async def test_initial_partial_first_of_two_never_persists(self):
        self.instances.append(self.instance('first'))
        await self.plugin.initialize()
        self.assertEqual(self.config['platform_id'],''); self.assertEqual(self.config.saves,0)
        self.instances.append(self.instance('second'))
        await self.plugin.on_astrbot_loaded()
        self.assertEqual(self.config['platform_id'],''); self.assertEqual(self.config['adapter'],'')
        self.assertEqual(self.plugin.discovery_status['state'],'multiple_candidates')
        self.assertEqual(self.config.saves,0)
    async def test_unique_fills_only_after_loaded_never_enables_or_infers_identity(self):
        self.instances.append(self.instance('instance-1'))
        await self.plugin.initialize(); self.assertEqual(self.config['platform_id'],'')
        await self.plugin.on_astrbot_loaded()
        self.assertEqual(self.config['platform_id'],'instance-1'); self.assertEqual(self.config['adapter'],'aiocqhttp')
        self.assertFalse(self.config['enabled']); self.assertIsNone(self.plugin.endpoint)
        for key in ('self_id','sender_id','mcp_token'): self.assertEqual(self.config[key],'')
        self.assertEqual(self.config['group_ids'],[])
        await self.plugin.on_astrbot_loaded(); self.assertEqual(self.config.saves,1)
    async def test_manual_edit_between_initialize_and_loaded_is_preserved(self):
        self.instances.append(self.instance('instance-1')); await self.plugin.initialize()
        self.config['platform_id']='manual'; self.config['adapter']='aiocqhttp'
        await self.plugin.on_astrbot_loaded()
        self.assertEqual(self.config['platform_id'],'manual'); self.assertEqual(self.config.saves,0)
    async def test_enabled_incomplete_start_waits_then_starts_once(self):
        self.config.update(enabled=True,self_id='100',sender_id='200',mcp_token='x'*32)
        await self.plugin.initialize(); self.assertFalse(self.plugin.ready)
        self.instances.append(self.instance('instance-1')); await self.plugin.on_astrbot_loaded()
        self.assertTrue(self.plugin.ready); endpoint=self.plugin.endpoint
        await self.plugin.on_astrbot_loaded(); self.assertIs(self.plugin.endpoint,endpoint)
    async def test_enabled_missing_sensitive_fields_still_fails_closed_after_discovery(self):
        self.config['enabled']=True; await self.plugin.initialize()
        self.instances.append(self.instance('instance-1')); await self.plugin.on_astrbot_loaded()
        self.assertFalse(self.plugin.ready); self.assertIsNone(self.plugin.host)
        self.assertEqual(self.config['self_id'],''); self.assertEqual(self.config['mcp_token'],'')
    async def test_optional_discovery_failure_does_not_break_manual_reload(self):
        self.config.update(enabled=True,platform_id='instance-1',adapter='aiocqhttp',
                           self_id='100',sender_id='200',mcp_token='x'*32)
        def fail(*args): raise RuntimeError('secret-config')
        self.module.discover_defaults=fail
        await self.plugin.initialize(); self.assertTrue(self.plugin.ready)
        self.assertEqual(self.plugin.discovery_status,{'state':'unavailable','candidate_ids':[]})
    async def test_missing_optional_hook_does_not_break_manual_module(self):
        original=Filter.on_astrbot_loaded
        del Filter.on_astrbot_loaded
        try:
            self.module=load_main(self.tmp.name)
        finally:
            Filter.on_astrbot_loaded=staticmethod(original)
        self.config.update(enabled=True,platform_id='instance-1',adapter='aiocqhttp',
                           self_id='100',sender_id='200',mcp_token='x'*32)
        self.plugin=self.module.Main(self.context,self.config)
        await self.plugin.initialize(); self.assertTrue(self.plugin.ready)

if __name__=='__main__': unittest.main()
