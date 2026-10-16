"""Old three-argument qq_reply compatibility, real local HTTP, synthetic bot only."""
import base64,hashlib,json,unittest
import test_media_request_limits as fixtures

class LegacyMediaRPCTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.MediaRequestLimitTests.asyncSetUp
    asyncTearDown=fixtures.MediaRequestLimitTests.asyncTearDown
    post=fixtures.MediaRequestLimitTests.post
    def envelope(self,value,key='compat'):
        return {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_reply','arguments':{
            'message_id':self.mid,'reply_key':key,'text':'DOT_MEDIA_V1\n'+json.dumps(value,separators=(',',':'))}}}
    async def test_old_argument_names_exact20mb_inline_dedupe_and_plus_one(self):
        data=b'x'*20_000_000;encoded=base64.b64encode(data).decode()
        body=self.envelope({'op':'inline','kind':'file','filename':'synthetic.bin','data_base64':encoded})
        for _ in range(2):
            status,result=await self.post(body);self.assertEqual(status,200)
            self.assertFalse(result['result']['isError'],result)
            self.assertEqual(result['result']['structuredContent']['state'],'adapter_completed')
        self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['size'],20_000_000)
        self.assertEqual(self.sent[0]['sha256'],hashlib.sha256(data).hexdigest())
        body=self.envelope({'op':'inline','kind':'file','filename':'synthetic.bin','data_base64':encoded[:-1]+'A'},'over')
        status,result=await self.post(body);self.assertEqual(status,200);self.assertTrue(result['result']['isError'])
        self.assertEqual(len(self.sent),1);self.assertEqual(self.endpoint._media_requests._value,2)
    async def test_large_ordinary_text_does_not_gain_media_permission(self):
        body=self.envelope({});body['params']['arguments']['text']='x'*100000
        status,result=await self.post(body);self.assertEqual(status,413);self.assertEqual(self.sent,[])
    async def test_reserved_malformed_envelope_is_not_plaintext(self):
        body=self.envelope({});body['params']['arguments']['text']='DOT_MEDIA_V1\n{"op":"inline","path":"/etc/passwd"}'
        status,result=await self.post(body);self.assertEqual(status,200);self.assertTrue(result['result']['isError']);self.assertEqual(self.sent,[])
    async def test_chunk_protocol_20mb_commit_and_retry_use_one_durable_send(self):
        data=b'z'*20_000_000
        status,res=await self.post(self.envelope({'op':'begin','kind':'file','filename':'large.bin','size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},'staged'))
        self.assertEqual(status,200);self.assertFalse(res['result']['isError'],res)
        upload=res['result']['structuredContent']['upload_id']
        for offset in range(0,len(data),262144):
            value={'op':'chunk','upload_id':upload,'offset':offset,'data_base64':base64.b64encode(data[offset:offset+262144]).decode()}
            status,res=await self.post(self.envelope(value,'staged'));self.assertEqual(status,200);self.assertFalse(res['result']['isError'],res)
        body=self.envelope({'op':'commit','upload_id':upload},'staged')
        status,res=await self.post(body);self.assertEqual(status,200);self.assertFalse(res['result']['isError'],res)
        self.assertEqual(res['result']['structuredContent']['state'],'adapter_completed');self.assertEqual(len(self.sent),1)
        status,res=await self.post(body);self.assertEqual(status,200);self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['sha256'],hashlib.sha256(data).hexdigest())
    async def test_staging_progress_and_commit_bypass_unrelated_heavy_media_waiters(self):
        import asyncio
        await self.endpoint._compat_media.acquire();await self.endpoint._compat_media.acquire()
        try:
            data=b'bounded staged content'
            _,res=await asyncio.wait_for(self.post(self.envelope({'op':'begin','kind':'file','filename':'a.txt','size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()},'hol')),1)
            upload=res['result']['structuredContent']['upload_id']
            _,res=await asyncio.wait_for(self.post(self.envelope({'op':'chunk','upload_id':upload,'offset':0,'data_base64':base64.b64encode(data).decode()},'hol')),1)
            self.assertFalse(res['result']['isError'])
            _,res=await asyncio.wait_for(self.post(self.envelope({'op':'commit','upload_id':upload},'hol')),1)
            self.assertEqual(res['result']['structuredContent']['state'],'adapter_completed')
        finally:self.endpoint._compat_media.release();self.endpoint._compat_media.release()
