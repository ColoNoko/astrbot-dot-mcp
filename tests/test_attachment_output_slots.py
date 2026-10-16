"""20 MB local RPC and bounded response lifecycle; no real source or user upload."""
import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch
import aiohttp
import test_attachment_resolution as fixtures


class AttachmentOutputTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown=fixtures.AttachmentResolutionTests.asyncTearDown
    event=fixtures.AttachmentResolutionTests.event
    capture=fixtures.AttachmentResolutionTests.capture

    def endpoint(self):
        spec=importlib.util.spec_from_file_location('plugin_mock.output_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module,module.Endpoint(self.plugin.host,'x'*32,['callbacks.example.com'],port=0)

    async def test_two_exact_20mb_files_round_trip_once_base64_in_real_local_rpc(self):
        body=b'\x89PNG\r\n\x1a\n'+b'\x00'*(20000000-8)
        digest=hashlib.sha256(body).hexdigest()
        self.transport.body=body
        a=await self.capture(self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS},kind='image'))
        b=await self.capture(self.event({'file_id':'file','url':fixtures.HTTPS},kind='file'))
        module,endpoint=self.endpoint();await endpoint.start()
        async def fetch(client,ids):
            mid,aid=ids
            async with client.post('http://127.0.0.1:'+str(endpoint.port)+'/mcp',headers={
                'Authorization':'Bearer '+'x'*32,'MCP-Protocol-Version':module.PROTOCOL_VERSION,'Mcp-Method':'tools/call'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_read_attachment',
                     'arguments':{'message_id':mid,'attachment_id':aid}}}) as response:
                raw=await response.read()
            self.assertLessEqual(len(raw),32*1024*1024)
            value=json.loads(raw)['result'];self.assertFalse(value['isError'])
            blobs=[c['data'] if c['type']=='image' else c['resource']['blob'] for c in value['content'] if c['type'] in ('image','resource')]
            self.assertEqual(len(blobs),1)
            self.assertEqual(len(blobs[0]),26666668)
            decoded=base64.b64decode(blobs[0]);self.assertEqual(len(decoded),20000000)
            self.assertEqual(hashlib.sha256(decoded).hexdigest(),digest)
            self.assertNotIn('blob',value.get('structuredContent',{}))
            return len(raw)
        try:
            async with aiohttp.ClientSession() as client:
                sizes=await asyncio.gather(fetch(client,a),fetch(client,b))
            self.assertEqual(len(self.transport.calls),2)
            self.assertEqual(endpoint._attachment_responses._value,2)
            print('20 MB local RPC response bytes:',sizes)
        finally:await endpoint.close()

    async def test_slots_cover_write_and_duplicates_wait_without_consuming_slots(self):
        module,endpoint=self.endpoint();endpoint._stopping=False
        gate={i:asyncio.Event() for i in range(4)};calls=[];active=0;peak=0
        class Response:
            body=b'{}'
            headers={}
            async def write(self,data):pass
            async def prepare(self,request):self.request=request
            async def write_eof(self):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                try:await gate[self.request.id].wait()
                finally:active-=1
            def force_close(self):pass
        async def dispatch(method,params):calls.append(params['arguments']['attachment_id']);return {'content':[]}
        def req(i):return types.SimpleNamespace(id=i,transport=types.SimpleNamespace(close=lambda:None))
        def params(a):return {'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+a*32}}
        tasks=[]
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',side_effect=lambda *a,**k:Response()):
            tasks.append(asyncio.create_task(endpoint._attachment_response(req(0),0,params('a'))))
            await asyncio.sleep(.01)
            tasks.append(asyncio.create_task(endpoint._attachment_response(req(1),1,params('a'))))
            tasks.append(asyncio.create_task(endpoint._attachment_response(req(2),2,params('b'))))
            tasks.append(asyncio.create_task(endpoint._attachment_response(req(3),3,params('c'))))
            await asyncio.sleep(.02)
            self.assertEqual(calls,['att_'+'a'*32,'att_'+'b'*32]);self.assertEqual(peak,2)
            gate[0].set();await asyncio.sleep(.02)
            self.assertEqual(calls[2],'att_'+'c'*32)
            for event in gate.values():event.set()
            await asyncio.gather(*tasks)
        self.assertEqual(peak,2);self.assertEqual(endpoint._attachment_responses._value,2)
        waits=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_response_queue_ms']
        self.assertEqual(len(waits),4)

    async def test_cancel_and_write_timeout_release_slots_and_close_transport(self):
        module,endpoint=self.endpoint();endpoint._stopping=False
        entered=asyncio.Event();closed=[]
        class Response:
            body=b'{}'
            headers={}
            async def write(self,data):pass
            async def prepare(self,request):pass
            async def write_eof(self):entered.set();await asyncio.Event().wait()
            def force_close(self):pass
        request=types.SimpleNamespace(transport=types.SimpleNamespace(close=lambda:closed.append(True)))
        params={'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+'b'*32}}
        async def dispatch(*args):return {'content':[]}
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',side_effect=lambda *a,**k:Response()):
            task=asyncio.create_task(endpoint._attachment_response(request,1,params))
            await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(endpoint._attachment_responses._value,2)
            with patch.object(module,'ATTACHMENT_WRITE_TIMEOUT',.01):
                await endpoint._attachment_response(request,2,params)
        self.assertEqual(len(closed),2);self.assertEqual(endpoint._attachment_responses._value,2)

    async def test_nonconnection_writer_exception_never_attempts_second_response(self):
        module,endpoint=self.endpoint();endpoint._stopping=False
        closed=[]
        class Response:
            body=b'{}'
            headers={}
            async def write(self,data):pass
            async def prepare(self,request):pass
            async def write_eof(self):raise RuntimeError('SECRET_WRITE_FAILURE')
            def force_close(self):pass
        request=types.SimpleNamespace(transport=types.SimpleNamespace(close=lambda:closed.append(True)))
        async def dispatch(*args):return {'content':[]}
        params={'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+'b'*32}}
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',side_effect=lambda *a,**k:Response()) as render:
            await endpoint._attachment_response(request,1,params)
        self.assertEqual(render.call_count,1);self.assertEqual(closed,[True])
        trace=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_responses'][-1]
        self.assertEqual(trace['outcome'],'failed');self.assertEqual(trace['error'],'write_failed')
        self.assertIn('http_write_eof',str(trace));self.assertNotIn('SECRET_WRITE_FAILURE',str(trace))
        self.assertEqual(endpoint._attachment_responses._value,2)

    async def test_serialization_failure_is_correlated_without_raw_exception(self):
        module,endpoint=self.endpoint();endpoint._stopping=False
        request=types.SimpleNamespace(transport=None)
        async def dispatch(*args):return {'content':[], 'structuredContent':{'read_id':'read_'+'c'*32}}
        params={'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+'b'*32}}
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',side_effect=ValueError('SECRET_JSON')):
            with self.assertRaises(ValueError):await endpoint._attachment_response(request,1,params)
        trace=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_responses'][-1]
        self.assertEqual(trace['read_id'],'read_'+'c'*32)
        self.assertEqual(trace['error'],'serialization_failed')
        self.assertNotIn('SECRET_JSON',str(trace))
        self.assertEqual(endpoint._attachment_responses._value,2)
