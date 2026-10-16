"""Native reader DNS diagnostics over authenticated loopback; no external lookup."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
import aiohttp
import test_attachment_resolution as resolution_tests


class AttachmentDNSRPCTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = resolution_tests.AttachmentResolutionTests.asyncSetUp
    asyncTearDown = resolution_tests.AttachmentResolutionTests.asyncTearDown
    event = resolution_tests.AttachmentResolutionTests.event
    capture = resolution_tests.AttachmentResolutionTests.capture

    async def test_dns_failure_preserves_safe_diagnostic_through_real_rpc(self):
        self.cache.transport = self.errors.AttachmentTransport()
        event = self.event()
        event.bot.call_action.return_value = {'url':'https://images.example.com/SECRET_PATH?token=SECRET_QUERY'}
        mid, aid = await self.capture(event)
        spec = importlib.util.spec_from_file_location('plugin_mock.dns_endpoint', Path(__file__).resolve().parents[1]/'endpoint.py')
        endpoint_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(endpoint_module)
        endpoint = endpoint_module.Endpoint(self.plugin.host, 'x'*32, ['callbacks.example.com'], port=0)
        await endpoint.start()
        try:
            async with aiohttp.ClientSession() as client:
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo', side_effect=socket.gaierror(-2,'SECRET_EXCEPTION')):
                    async with client.post('http://127.0.0.1:'+str(endpoint.port)+'/mcp', headers={
                        'Authorization':'Bearer '+'x'*32, 'MCP-Protocol-Version':endpoint_module.PROTOCOL_VERSION,
                        'Mcp-Method':'tools/call'}, json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                         'params':{'name':'qq_read_attachment','arguments':{'message_id':mid,'attachment_id':aid}}}) as response:
                        self.assertEqual(response.status,200); result=(await response.json())['result']
            self.assertTrue(result['isError'])
            error=json.loads(result['content'][0]['text'])
            self.assertEqual(error['error'],'attachment_dns_failed')
            self.assertEqual(error['dns_diagnostic']['mode'],'system')
            self.assertEqual(error['dns_diagnostic']['hostname'],'images.example.com')
            self.assertEqual(error['dns_diagnostic']['reason'],'gaierror')
            self.assertNotIn('SECRET_',json.dumps(result))
            self.assertEqual(self.cache.list(mid)[0]['state'],'unfetched')
        finally:
            await endpoint.close()

    async def test_mode_changed_before_read_is_fail_closed(self):
        self.cache.transport = self.errors.AttachmentTransport()
        event=self.event(); mid,aid=await self.capture(event)
        self.config['attachment_dns']='google'
        with self.assertRaisesRegex(Exception,'binding_inactive'):
            await self.plugin._read_attachment(mid,aid)
        event.bot.call_action.assert_not_called()

    async def test_mode_changed_during_adapter_query_is_fail_closed(self):
        self.cache.transport = self.errors.AttachmentTransport()
        event=self.event(); mid,aid=await self.capture(event)
        async def query(**kwargs):
            self.config['attachment_dns']='google'
            return {'url':'https://images.example.com/SECRET'}
        event.bot.call_action.side_effect=query
        with self.assertRaisesRegex(Exception,'attachment_source_unavailable'):
            await self.plugin._read_attachment(mid,aid)
        self.assertEqual(self.cache.list(mid)[0]['state'], 'unfetched')
