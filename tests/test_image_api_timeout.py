"""Bounded optional image API refresh; synthetic captured URLs and fake downloads."""
import asyncio
import unittest
from unittest.mock import patch
import test_attachment_resolution as fixtures


class ImageAPITimeoutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = fixtures.AttachmentResolutionTests.asyncTearDown
    event = fixtures.AttachmentResolutionTests.event
    capture = fixtures.AttachmentResolutionTests.capture
    read = fixtures.AttachmentResolutionTests.read

    async def test_hung_optional_image_api_is_cancelled_before_original_https_fallback(self):
        cancelled=asyncio.Event()
        async def query(**kwargs):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS})
        event.bot.call_action.side_effect=query
        result=await self.read(event)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(result['isError'])
        self.assertEqual(self.transport.calls,[fixtures.HTTPS])

    async def test_revoked_on_api_cancellation_does_not_use_original_url(self):
        async def query(**kwargs):
            try: await asyncio.Event().wait()
            finally: self.config['enabled']=False
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS})
        event.bot.call_action.side_effect=query
        with self.assertRaisesRegex(Exception,'attachment_source_unavailable'):
            await self.read(event)
        self.assertEqual(self.transport.calls,[])

    async def test_no_original_https_does_not_get_optional_short_timeout(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTP})
        real_wait_for=asyncio.wait_for
        timeouts=[]
        async def wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await real_wait_for(awaitable,timeout)
        with patch.object(self.module.asyncio,'wait_for',side_effect=wait_for):
            result=await self.read(event)
        self.assertFalse(result['isError'])
        self.assertNotIn(3.0,timeouts)
        self.assertEqual(self.transport.calls,[fixtures.API_HTTPS])

    async def test_file_api_not_subject_to_image_short_timeout(self):
        event=self.event({'file_id':'file-id','url':fixtures.HTTPS},kind='file')
        real_wait_for=asyncio.wait_for; timeouts=[]
        async def wait_for(awaitable,timeout):
            timeouts.append(timeout)
            return await real_wait_for(awaitable,timeout)
        with patch.object(self.module.asyncio,'wait_for',side_effect=wait_for):
            result=await self.read(event)
        self.assertFalse(result['isError'])
        self.assertNotIn(3.0,timeouts)
        self.assertEqual(self.transport.calls,[fixtures.API_HTTPS])

    async def test_proxy_changed_during_query_prevents_fallback(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS})
        async def query(**kwargs):
            self.config.update(proxy_enabled=True,proxy_url='http://127.0.0.1:7890')
            raise RuntimeError('not public')
        event.bot.call_action.side_effect=query
        with self.assertRaisesRegex(Exception,'attachment_source_unavailable'):
            await self.read(event)
        self.assertEqual(self.transport.calls,[])

    async def test_changed_source_during_query_prevents_fallback(self):
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTPS})
        async def query(**kwargs):
            event.message_obj.raw_message['message'][1]['data']['url']='https://other.example.com/x'
            raise RuntimeError('not public')
        event.bot.call_action.side_effect=query
        with self.assertRaisesRegex(Exception,'attachment_source_unavailable'):
            await self.read(event)
        self.assertEqual(self.transport.calls,[])

    async def test_api_timeout_diagnostic_and_runtime_status_survive_authenticated_rpc(self):
        import importlib.util
        import json
        from pathlib import Path
        import aiohttp
        event=self.event({'file':fixtures.TOKEN,'url':fixtures.HTTP})
        cancelled=asyncio.Event()
        async def query(**kwargs):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        event.bot.call_action.side_effect=query
        mid,aid=await self.capture(event)
        spec=importlib.util.spec_from_file_location('plugin_mock.stage_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        endpoint_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(endpoint_module)
        endpoint=endpoint_module.Endpoint(self.plugin.host,'x'*32,['callbacks.example.com'],port=0)
        await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                async def rpc(name,args):
                    async with client.post('http://127.0.0.1:'+str(endpoint.port)+'/mcp',headers={
                        'Authorization':'Bearer '+'x'*32,'MCP-Protocol-Version':endpoint_module.PROTOCOL_VERSION,
                        'Mcp-Method':'tools/call'},json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                         'params':{'name':name,'arguments':args}}) as response:
                        return (await response.json())['result']
                with patch.object(self.errors,'NETWORK_TIMEOUT',.02):
                    result=await rpc('qq_read_attachment',{'message_id':mid,'attachment_id':aid})
                self.assertTrue(cancelled.is_set())
                self.assertTrue(result['isError'])
                failure=json.loads(result['content'][0]['text'])
                self.assertEqual(failure['error'],'attachment_timeout')
                diagnostic=failure['transport_diagnostic']
                self.assertEqual(diagnostic['stage'],'adapter_api')
                self.assertEqual(diagnostic['stages'][-1]['api_action'],'get_image')
                self.assertEqual(diagnostic['stages'][-1]['outcome'],'timeout')
                self.assertNotIn('secret=',json.dumps(failure))
                status=(await rpc('qq_status',{}))['structuredContent']
                # Inspect exactly the safe attachment summary returned by Store.status.
                summary=status['attachments']
                self.assertEqual(summary['last_read']['read_id'],diagnostic['read_id'])
                self.assertIs(summary['proxy_enabled'],False)
        finally:
            await endpoint.close()
