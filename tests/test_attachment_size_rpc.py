"""Early captured/API size checks and safe user-facing errors; synthetic only."""
import importlib.util
import json
from pathlib import Path
import unittest
import aiohttp
import test_attachment_resolution as fixtures


class AttachmentSizeRPCTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = fixtures.AttachmentResolutionTests.asyncTearDown
    event = fixtures.AttachmentResolutionTests.event
    capture = fixtures.AttachmentResolutionTests.capture
    read = fixtures.AttachmentResolutionTests.read

    async def test_raw_known_oversize_image_and_file_reject_before_api(self):
        for kind in ('image','file'):
            event=self.event({'file':fixtures.TOKEN,'file_id':'captured-file',
                              'url':fixtures.HTTPS,'file_size':20000001},kind=kind)
            with self.assertRaisesRegex(Exception,'attachment_too_large') as failure:
                await self.read(event)
            self.assertEqual(failure.exception.size_diagnostic,
                {'max_bytes':20000000,'known_size_bytes':20000001})
            event.bot.call_action.assert_not_called()
        self.assertEqual(self.transport.calls,[])

    async def test_exact_known_limit_is_allowed_but_not_trusted_as_actual_size(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS,'file_size':20000000})
        result=await self.read(event)
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.transport.calls),1)

    async def test_api_known_oversize_rejects_without_fallback_download(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS})
        event.bot.call_action.return_value={'url':fixtures.API_HTTPS,'size':'20000001'}
        with self.assertRaisesRegex(Exception,'attachment_too_large'):
            await self.read(event)
        self.assertEqual(self.transport.calls,[])

    async def test_real_rpc_oversize_error_is_chinese_and_contains_no_source(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS,'size':20000001})
        mid,aid=await self.capture(event)
        spec=importlib.util.spec_from_file_location('plugin_mock.size_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        ep=importlib.util.module_from_spec(spec);spec.loader.exec_module(ep)
        endpoint=ep.Endpoint(self.plugin.host,'x'*32,['callbacks.example.com'],port=0)
        await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post('http://127.0.0.1:'+str(endpoint.port)+'/mcp',headers={
                    'Authorization':'Bearer '+'x'*32,'MCP-Protocol-Version':ep.PROTOCOL_VERSION,'Mcp-Method':'tools/call'},
                    json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'qq_read_attachment',
                          'arguments':{'message_id':mid,'attachment_id':aid}}}) as response:
                    result=(await response.json())['result']
            self.assertTrue(result['isError'])
            failure=json.loads(result['content'][0]['text'])
            self.assertEqual(failure['error'],'attachment_too_large')
            self.assertIn('20,000,000',failure['message'])
            self.assertEqual(failure['size_diagnostic']['max_bytes'],20000000)
            self.assertNotIn('secret=',json.dumps(failure))
            self.assertEqual(self.transport.calls,[])
        finally:await endpoint.close()
