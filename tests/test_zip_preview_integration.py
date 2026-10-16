import base64,io,json,unittest,zipfile,types,importlib
import aiohttp
import test_attachment_output_slots as output_fixtures

class ZipPreviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=output_fixtures.AttachmentOutputTests.asyncSetUp
    asyncTearDown=output_fixtures.AttachmentOutputTests.asyncTearDown
    event=output_fixtures.AttachmentOutputTests.event
    capture=output_fixtures.AttachmentOutputTests.capture
    endpoint=output_fixtures.AttachmentOutputTests.endpoint
    def archive(self,large=False):
        value=io.BytesIO()
        with zipfile.ZipFile(value,'w',zipfile.ZIP_STORED) as archive:
            archive.writestr('README.txt',b'not read or executed')
            archive.writestr('../unsafe.exe',b'x'*(8_764_000 if large else 16))
        return value.getvalue()
    async def test_real_rpc_large_zip_default_raw_and_explicit_listing_only(self):
        body=self.archive(large=True);self.transport.body=body
        event=self.event({'file_id':'captured-file','url':'https://media.example.com/file'},kind='file')
        mid,aid=await self.capture(event)
        logs=[]
        helper=importlib.import_module('plugin_mock.response_timing').ResponseTimingLogger(types.SimpleNamespace(info=logs.append))
        self.plugin.response_logger=helper
        module,endpoint=self.endpoint();endpoint.response_logger=helper
        await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                async def rpc(extra):
                    async with client.post(f'http://127.0.0.1:{endpoint.port}/mcp',headers={
                        'Authorization':'Bearer '+'x'*32,'MCP-Protocol-Version':module.PROTOCOL_VERSION,'Mcp-Method':'tools/call'},json={
                        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_read_attachment','arguments':{'message_id':mid,'attachment_id':aid,**extra}}}) as response:
                        raw=await response.read();self.assertEqual(response.status,200);return raw,json.loads(raw)['result']
                raw,result=await rpc({'include_raw':False})
                self.assertFalse(result['isError']);self.assertLess(len(raw),100000)
                self.assertTrue(all(c['type']=='text' for c in result['content']))
                self.assertEqual(result['structuredContent']['delivery_mode'],'listing_only')
                self.assertFalse(result['structuredContent']['raw_returned'])
                listing=next(json.loads(c['text']) for c in result['content'] if json.loads(c['text']).get('type')=='untrusted_zip_directory')
                self.assertEqual([e['name'] for e in listing['directory']['entries']],['README.txt','../unsafe.exe'])
                self.assertTrue(listing['directory']['entries'][1]['unsafe_path'])
                _,result=await rpc({})
                self.assertEqual(result['structuredContent']['delivery_mode'],'directory_and_raw')
                self.assertEqual(result['structuredContent']['text_preview']['reason'],'zip_directory_only')
                self.assertTrue(result['structuredContent']['raw_returned'])
                raw_listing=next(json.loads(c['text']) for c in result['content'] if c['type']=='text' and json.loads(c['text']).get('type')=='untrusted_zip_directory')
                self.assertFalse(raw_listing['listing_only']);self.assertTrue(raw_listing['raw_returned'])
                self.assertEqual(len(raw_listing['directory']['entries']),2)
                resource=next(c['resource'] for c in result['content'] if c['type']=='resource')
                self.assertEqual(base64.b64decode(resource['blob']),body)
                records=self.plugin.host.store.message_timing.status()['recent'][-1]['attachment_responses']
                self.assertTrue(any(r['scope']=='main' for r in records))
                endpoints=[r for r in records if r['scope']=='endpoint']
                self.assertTrue(all(r['outcome']=='succeeded' for r in endpoints))
                self.assertTrue(all(r['encoded_bytes']>0 for r in endpoints))
                self.assertGreaterEqual(len(logs),4)
                self.assertIn(mid,' '.join(logs))
                self.assertIn('http_write_eof',' '.join(logs))
                self.assertNotIn('README.txt',' '.join(logs))
                self.assertNotIn('unsafe.exe',' '.join(logs))
                self.assertNotIn('media.example.com',' '.join(logs))
                self.assertNotIn('base64',' '.join(logs))
        finally:await endpoint.close()
    async def test_corrupt_zip_has_explicit_directory_error_no_raw_fallback(self):
        self.transport.body=b'PK\x03\x04'+b'not a real zip'
        mid,aid=await self.capture(self.event({'file_id':'f','url':'https://media.example.com/file'},kind='file'))
        result=await self.plugin._read_attachment(mid,aid,include_raw=False)
        self.assertTrue(all(c['type']=='text' for c in result['content']))
        listing=next(json.loads(c['text']) for c in result['content'] if json.loads(c['text']).get('type')=='untrusted_zip_directory')
        self.assertFalse(listing['directory']['available']);self.assertFalse(listing['raw_returned'])
