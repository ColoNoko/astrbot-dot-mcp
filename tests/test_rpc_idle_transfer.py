import asyncio,json,types,unittest
from unittest.mock import patch
import test_endpoint as ep
import test_attachment_output_slots as output

class InputIdleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ep.EndpointTests.asyncSetUp
    asyncTearDown=ep.EndpointTests.asyncTearDown
    def headers(self):return {'Authorization':'Bearer '+ep.TOKEN,'Content-Type':'application/json','MCP-Protocol-Version':ep.endpoint_module.PROTOCOL_VERSION,'Mcp-Method':'tools/call'}
    async def test_progressing_chunked_input_exceeds_scaled_idle_window(self):
        body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_status','arguments':{}}}).encode()
        async def chunks():
            for i in range(0,len(body),8):
                yield body[i:i+8];await asyncio.sleep(.01)
        with patch.object(ep.endpoint_module,'READ_TIMEOUT',.035):
            async with self.client.post(self.url,headers=self.headers(),data=chunks()) as response:
                self.assertEqual(response.status,200);self.assertIn('result',await response.json())
    async def test_input_stall_stops_without_reset(self):
        async def chunks():
            yield b'{';await asyncio.sleep(.1);yield b'}'
        with patch.object(ep.endpoint_module,'READ_TIMEOUT',.02):
            async with self.client.post(self.url,headers=self.headers(),data=chunks()) as response:
                self.assertEqual(response.status,408)
        self.assertEqual(self.endpoint._media_requests._value,2)

class OutputIdleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=output.AttachmentOutputTests.asyncSetUp
    asyncTearDown=output.AttachmentOutputTests.asyncTearDown
    endpoint=output.AttachmentOutputTests.endpoint
    async def test_per_chunk_write_allows_total_longer_than_idle(self):
        module,endpoint=self.endpoint();endpoint._stopping=False
        writes=[];closed=[]
        class Response:
            body=b'abcdefghijklmnopqrst';headers={}
            async def prepare(self,request):pass
            async def write(self,data):await asyncio.sleep(.01);writes.append(data)
            async def write_eof(self):pass
            def force_close(self):closed.append(True)
        request=types.SimpleNamespace(transport=types.SimpleNamespace(close=lambda:closed.append(True)))
        async def dispatch(*args):return {'content':[]}
        args={'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+'b'*32}}
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',side_effect=lambda *a,**k:Response()),patch.object(module,'ATTACHMENT_WRITE_TIMEOUT',.035),patch.object(module,'ATTACHMENT_WRITE_CHUNK',2):
            await endpoint._attachment_response(request,1,args)
        self.assertEqual(b''.join(writes),b'abcdefghijklmnopqrst');self.assertEqual(closed,[])
        trace=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_responses'][-1]
        self.assertEqual(trace['outcome'],'succeeded');self.assertIn('http_write',str(trace));self.assertGreater(trace['elapsed_ms'],35)
    async def test_stalled_write_closes_and_releases(self):
        module,endpoint=self.endpoint();endpoint._stopping=False;closed=[]
        class Response:
            body=b'bytes';headers={}
            async def prepare(self,request):pass
            async def write(self,data):await asyncio.Event().wait()
            async def write_eof(self):raise AssertionError('must not finish')
            def force_close(self):closed.append(True)
        request=types.SimpleNamespace(transport=types.SimpleNamespace(close=lambda:closed.append(True)))
        async def dispatch(*args):return {'content':[]}
        args={'name':'qq_read_attachment','arguments':{'message_id':'msg_'+'a'*32,'attachment_id':'att_'+'b'*32}}
        with patch.object(endpoint,'_dispatch',side_effect=dispatch),patch.object(module,'_json_response',return_value=Response()),patch.object(module,'ATTACHMENT_WRITE_TIMEOUT',.02):
            await endpoint._attachment_response(request,1,args)
        self.assertTrue(closed);self.assertEqual(endpoint._attachment_responses._value,2)
        trace=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_responses'][-1]
        self.assertEqual(trace['error'],'timeout');self.assertIn('http_write',str(trace))
