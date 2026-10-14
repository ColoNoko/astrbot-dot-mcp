import copy
import types
import unittest
from test_main import load_main
import tempfile
from pathlib import Path

class Config(dict):
    def save_config(self):
        self.saved = copy.deepcopy(dict(self))

class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=False, platform_id='instance-1',adapter='aiocqhttp',
                             self_id='100',sender_id='200',mcp_token='DUMMY_'+'x'*40,
                             group_ids=['300'],callback_hosts=[])
        self.p = self.module.Main(object(), self.config)
        self.req = types.SimpleNamespace(username='local-admin',plugin_name='astrbot_plugin_dot_private')
        await self.p.initialize()
    async def asyncTearDown(self):
        await self.p.terminate(); self.tmp.cleanup()
    def payload(self, **kwargs):
        status=self.p.admin.status(self.req)
        return dict(csrf_token=status['csrf_token'],revision=status['revision'],**kwargs)
    async def test_redacted_status_preserves_existing_config(self):
        result=self.p.admin.status(self.req)
        self.assertTrue(result['secrets']['mcp_token_set'])
        self.assertNotIn(self.config['mcp_token'],str(result))
        self.assertFalse(self.p.ready)
    async def test_save_advanced_keeps_simple_and_blank_secret(self):
        old=self.config['mcp_token']
        await self.p.admin.mutate(self.req,self.payload(settings={'listen_port':8770}),'settings')
        self.assertEqual(self.config['mcp_token'],old)
        self.assertEqual(self.config['group_ids'],['300'])
        self.assertEqual(self.config['listen_port'],8770)
        self.assertFalse(self.config['enabled'])
    async def test_auth_csrf_and_revision(self):
        for req in (types.SimpleNamespace(username=None,plugin_name=self.req.plugin_name),
                    types.SimpleNamespace(username='x',plugin_name='other')):
            with self.assertRaisesRegex(Exception,'forbidden'): self.p.admin.status(req)
        for update,code in (({'csrf_token':'bad'},'forbidden'),({'revision':'stale'},'revision_conflict')):
            payload=self.payload(settings={}); payload.update(update)
            with self.assertRaisesRegex(Exception,code):
                await self.p.admin.mutate(self.req,payload,'settings')
    async def test_wrong_user_cannot_reuse_csrf(self):
        req=types.SimpleNamespace(username='other',plugin_name=self.req.plugin_name)
        with self.assertRaisesRegex(Exception,'forbidden'):
            await self.p.admin.mutate(req,self.payload(settings={}), 'settings')
    async def test_invalid_never_saves(self):
        for settings in ({'enabled':True},{'listen_host':'0.0.0.0'}, {'listen_port':True},
                         {'callback_hosts':['https://evil.example/x']}, {'tunnel_id':'x\nsecret'},
                         {'adapter':'telegram'}, {'self_id':'True'}, {'platform_id':'a\nb'}):
            with self.assertRaisesRegex(Exception,'invalid_settings'):
                await self.p.admin.mutate(self.req,self.payload(settings=settings),'settings')
        self.assertFalse(hasattr(self.config,'saved'))
    async def test_manual_mcp_auth_controls_removed(self):
        with self.assertRaisesRegex(Exception,'invalid_settings'):
            await self.p.admin.mutate(self.req,self.payload(action='generate_mcp_token'),'action')
        with self.assertRaisesRegex(Exception,'invalid_settings'):
            await self.p.admin.mutate(self.req,self.payload(settings={},mcp_token='x'*48),'settings')
    async def test_managed_auth_get_does_not_create_start_reuses(self):
        self.config['mcp_token']=''
        self.p.admin.status(self.req)
        self.assertEqual(self.config['mcp_token'],'')
        self.config['enabled']=True
        result=await self.p.admin.mutate(self.req,self.payload(action='apply'),'action')
        token=self.config['mcp_token']
        self.assertGreaterEqual(len(token),32); self.assertNotIn(token,str(result))
        await self.p.admin.mutate(self.req,self.payload(action='apply'),'action')
        self.assertEqual(self.config['mcp_token'],token)
    async def test_managed_auth_failed_persistence_stays_off(self):
        self.config['mcp_token']=''; self.config['enabled']=True
        def fail(): raise RuntimeError('secret')
        self.config.save_config=fail
        with self.assertRaisesRegex(Exception,'runtime_failed'):
            await self.p.admin.mutate(self.req,self.payload(action='apply'),'action')
        self.assertEqual(self.config['mcp_token'],''); self.assertFalse(self.p.ready)
    async def test_proxy_username_only_auth_explicit_reentry(self):
        self.config.update(proxy_url='http://127.0.0.1:7890',proxy_username='DUMMY_USER',proxy_password='')
        await self.p.admin.mutate(self.req,self.payload(settings={'proxy_url':'http://127.0.0.2:7890'},
            proxy_username='DUMMY_USER'),'settings')
        self.assertEqual(self.config['proxy_url'],'http://127.0.0.2:7890')
        self.assertEqual(self.config['proxy_password'],'')
    async def test_invalid_stored_proxy_url_never_echoes_credentials(self):
        self.config['proxy_url']='http://synthetic-user:synthetic-password@host:8080'
        result=self.p.admin.status(self.req)
        self.assertEqual(result['settings']['proxy_url'],'')
        self.assertNotIn('synthetic-password',str(result))
        self.assertEqual(result['runtime']['error_code'],'invalid_proxy_settings')
    async def test_proxy_credentials_retained_redacted_and_clearable(self):
        result=await self.p.admin.mutate(self.req,self.payload(settings={'proxy_enabled':True,'proxy_url':'http://127.0.0.1:7890'},
            proxy_username='DUMMY_USER',proxy_password='DUMMY_PASSWORD'),'settings')
        self.assertNotIn('DUMMY_PASSWORD',str(result)); self.assertTrue(result['secrets']['proxy_password_set'])
        await self.p.admin.mutate(self.req,self.payload(settings={}),'settings')
        self.assertEqual(self.config['proxy_password'],'DUMMY_PASSWORD')
        with self.assertRaisesRegex(Exception,'proxy_auth_reentry_required'):
            await self.p.admin.mutate(self.req,self.payload(settings={'proxy_url':'http://127.0.0.2:7890'}),'settings')
        await self.p.admin.mutate(self.req,self.payload(settings={'proxy_url':'http://127.0.0.2:7890'},clear_proxy_auth=True),'settings')
        self.assertEqual(self.config['proxy_password'],''); self.assertEqual(self.config['proxy_username'],'')
    async def test_key_replacement_redacted_and_no_enable(self):
        key='DUMMY_RUNTIME_'+'y'*40
        result=await self.p.admin.mutate(self.req,self.payload(settings={},tunnel_key=key),'settings')
        self.assertTrue(result['secrets']['tunnel_key_set'])
        self.assertNotIn(key,str(result))
        self.assertFalse(self.config['enabled'])
    async def test_failed_save_restores_values(self):
        def fail(): raise RuntimeError('secret-raw-error')
        self.config.save_config=fail
        with self.assertRaisesRegex(Exception,'^save_failed$'):
            await self.p.admin.mutate(self.req,self.payload(settings={'listen_port':8770}),'settings')
        self.assertNotIn('listen_port',self.config)
    async def test_config_change_invalidates_stale_page(self):
        payload=self.payload(settings={}); self.config['group_ids']=[]
        with self.assertRaisesRegex(Exception,'revision_conflict'):
            await self.p.admin.mutate(self.req,payload,'settings')
    async def test_stop_retains_error_but_not_stale_running_state(self):
        class Tunnel:
            closed=False
            def status(self): return {'state':'stopped' if self.closed else 'running',
                'process_running':not self.closed,'healthy':not self.closed,'ready':False,
                'last_error_code':None if self.closed else 'not_ready'}
            async def close(self): self.closed=True
        self.p.tunnel=Tunnel(); await self.p._stop_runtime()
        status=self.p.admin.status(self.req)['runtime']['tunnel']
        self.assertFalse(status['process_running']); self.assertFalse(status['healthy'])
        self.assertEqual(status['last_error_code'],'not_ready')
    async def test_cancelled_tunnel_close_still_closes_listener_store(self):
        import asyncio
        self.config['enabled']=True; await self.p._start_enabled()
        host=self.p.host
        class Tunnel:
            def status(self): return {'state':'stopped'}
            async def close(self): raise asyncio.CancelledError()
        self.p.tunnel=Tunnel()
        with self.assertRaises(asyncio.CancelledError): await self.p._stop_runtime()
        self.assertIsNone(self.p.host); self.assertIsNone(self.p.endpoint)
        with self.assertRaises(Exception): host.store.status()
    async def test_runtime_key_write_never_truncates_hardlink(self):
        import os
        root=Path(self.tmp.name); other=root/'other'; other.write_text('unchanged')
        path=root/'tunnel-runtime-key'; os.link(other,path)
        self.p._write_runtime_key(root,'DUMMY_LOCAL_KEY')
        self.assertEqual(other.read_text(),'unchanged')
        self.assertEqual(path.read_text(),'DUMMY_LOCAL_KEY')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
    async def test_config_change_during_stop_not_overwritten(self):
        original=self.p._stop_runtime
        async def stop():
            await original()
            self.config['self_id']='999'
        self.p._stop_runtime=stop
        with self.assertRaisesRegex(Exception,'revision_conflict'):
            await self.p.admin.mutate(self.req,self.payload(settings={'listen_port':8770}),'settings')
        self.assertEqual(self.config['self_id'],'999')
        self.assertNotIn('listen_port',self.config)
    async def test_mcp_key_limit_matches_listener(self):
        with self.assertRaisesRegex(Exception,'invalid_settings'):
            await self.p.admin.mutate(self.req,self.payload(settings={},mcp_token='x'*1025),'settings')
    async def test_failed_tunnel_stop_retains_handle_and_blocks_start(self):
        class Tunnel:
            def status(self): return {'state':'error'}
            async def close(self): raise RuntimeError('rawsecret')
        t=Tunnel(); self.p.tunnel=t
        with self.assertRaisesRegex(RuntimeError,'^tunnel_stop_incomplete$'):
            await self.p._stop_runtime()
        self.assertIs(self.p.tunnel,t)
        with self.assertRaisesRegex(RuntimeError,'^tunnel_stop_incomplete$'):
            await self.p._start_enabled()
        self.p.tunnel=None
    async def test_terminate_waits_for_delayed_start_then_stops(self):
        import asyncio
        entered=asyncio.Event(); release=asyncio.Event()
        original=self.module.Endpoint
        class Delayed(original):
            async def start(self): entered.set(); await release.wait()
        self.module.Endpoint=Delayed; self.config['enabled']=True
        start=asyncio.create_task(self.p._start_enabled())
        await entered.wait()
        stop=asyncio.create_task(self.p.terminate())
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        release.set(); await start; await stop
        self.assertFalse(self.p.ready); self.assertIsNone(self.p.endpoint)
        self.module.Endpoint=original
    async def test_closed_page_rejects(self):
        await self.p.terminate()
        with self.assertRaisesRegex(Exception,'forbidden'): self.p.admin.status(self.req)
    async def test_disabled_apply_never_starts(self):
        await self.p.admin.mutate(self.req,self.payload(action='apply'),'action')
        self.assertFalse(self.p.ready); self.assertIsNone(self.p.tunnel)

if __name__=='__main__': unittest.main()
