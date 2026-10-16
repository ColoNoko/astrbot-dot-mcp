import json,unittest
import test_media_request_limits as fixtures

class RemoteDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.MediaRequestLimitTests.asyncSetUp
    asyncTearDown=fixtures.MediaRequestLimitTests.asyncTearDown
    post=fixtures.MediaRequestLimitTests.post
    async def call(self,text,headers=None):
        body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_reply','arguments':{'message_id':self.mid,'reply_key':'diagnostic','text':text}}}
        return await self.post(body) if headers is None else await self.client.post(self.url,json=body,headers=headers)
    async def test_disabled_and_non_boolean_fail_closed(self):
        for value in (None,False,'true',1):
            self.plugin.config['remote_diagnostics_enabled']=value
            _,r=await self.call('DOT_DIAG_V1\n{"command":"version"}')
            self.assertTrue(r['result']['isError']);self.assertEqual(self.sent,[])
    async def test_three_commands_real_rpc_no_send_or_reply_claim(self):
        self.plugin.config['remote_diagnostics_enabled']=True
        for command in ('version','tools','status'):
            _,r=await self.call('DOT_DIAG_V1\n'+json.dumps({'command':command}))
            self.assertFalse(r['result']['isError'],r)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.plugin.host.store.status()['replies'],{})
    async def test_unknown_duplicate_extra_and_reserved_version_rejected(self):
        self.plugin.config['remote_diagnostics_enabled']=True
        for text in ['DOT_DIAG_V2\n{}','DOT_DIAG_V1\n{"command":"shell"}','DOT_DIAG_V1\n{"command":"status","url":"https://example.com"}','DOT_DIAG_V1\n{"command":"status","command":"status"}','DOT_DIAG_V1\n[]']:
            _,r=await self.call(text);self.assertTrue(r['result']['isError'],r)
        self.assertEqual(self.sent,[])
    async def test_current_revocation_and_non_owner_rejected(self):
        self.plugin.config['remote_diagnostics_enabled']=True
        self.plugin.config['enabled']=False
        _,r=await self.call('DOT_DIAG_V1\n{"command":"status"}')
        self.assertTrue(r['result']['isError'])
        self.plugin.config['enabled']=True
        original=self.plugin.host.store.message
        self.plugin.host.store.message=lambda mid:{**original(mid),'sender_id':'999'}
        _,r=await self.call('DOT_DIAG_V1\n{"command":"status"}')
        self.assertTrue(r['result']['isError']);self.assertEqual(self.sent,[])
    async def test_native_config_default_and_bearer_required(self):
        from pathlib import Path
        schema=json.loads((Path(__file__).resolve().parents[1]/'_conf_schema.json').read_text())
        self.assertIs(schema['remote_diagnostics_enabled']['default'],False)
        response=await self.call('DOT_DIAG_V1\n{"command":"status"}',{'Content-Type':'application/json'})
        self.assertEqual(response.status,401);await response.read();self.assertEqual(self.sent,[])
