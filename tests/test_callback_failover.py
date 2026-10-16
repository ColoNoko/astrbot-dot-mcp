"""Synthetic address selection/failover; no external proxy or callback."""
import asyncio
import errno
import socket
import unittest
from unittest.mock import AsyncMock, patch
import aiohttp
from test_callback_proxy import transport_module, EventError, PROXY, URL, entry, fake_session

class CallbackFailoverTests(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        return transport_module.CallbackTransport(['callbacks.example.com'],proxy=PROXY)
    def addresses(self):
        v4=entry();v6={**v4,'host':'2606:4700::1111','family':socket.AF_INET6}
        return [v6,v4]
    async def test_ipv4_preferred_and_vetted_alternative_keeps_signed_bytes(self):
        transport=self.transport();calls=[]
        async def once(url,body,headers,entries,test,timeout,detail):
            calls.append((url,body,dict(headers),entries[0]['host']))
            if len(calls)==1:raise EventError('callback_proxy_failed',http_status=502)
            return 200,b'ok'
        with patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            self.assertEqual(await transport.post(URL,b'EXACT SIGNED BYTES',{'webhook-id':'same'}),(200,b'ok'))
        self.assertEqual(calls[0][3],entry()['host'])
        self.assertEqual(calls[1][3],'2606:4700::1111')
        self.assertEqual(calls[0][:3],calls[1][:3])
        self.assertEqual([v['address_family'] for v in transport.last_transport['attempts']],['ipv4','ipv6'])
        self.assertNotIn('2606:',str(transport.last_transport))
        self.assertNotIn('callbacks.',str(transport.last_transport))
    async def test_tls_auth_and_proxy_connect_errors_never_try_other_target(self):
        for code,status in [('callback_tls_failed',None),('callback_proxy_failed',407),('callback_proxy_connect_failed',None),('callback_server_disconnected',None),
                            ('callback_connection_reset',None),('callback_network_failed',None),('callback_timeout',None)]:
            transport=self.transport();once=AsyncMock(side_effect=EventError(code,http_status=status))
            with patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
                with self.assertRaises(EventError):await transport.post(URL,b'{}',{})
            self.assertEqual(once.await_count,1)
    async def test_proxy_gateway_error_can_try_other_vetted_address(self):
        transport=self.transport();once=AsyncMock(side_effect=[EventError('callback_proxy_failed',http_status=502),(200,b'ok')])
        with patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            await transport.post(URL,b'{}',{})
        self.assertEqual(once.await_count,2)
    async def test_total_deadline_bounds_all_attempts(self):
        transport=self.transport()
        async def once(*args):await asyncio.sleep(1)
        with patch.object(transport_module,'NETWORK_TIMEOUT',.02),patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            with self.assertRaisesRegex(EventError,'callback_timeout'):await transport.post(URL,b'{}',{})
        self.assertEqual(transport.last_transport['attempts'][-1]['error'],'callback_timeout')
    async def test_classified_errors_and_errno_do_not_echo_exception(self):
        failures=[(aiohttp.ClientProxyConnectionError(None,OSError(errno.ECONNREFUSED,'SECRET_SENTINEL')),'callback_proxy_connect_failed','ECONNREFUSED'),
                  (aiohttp.ServerDisconnectedError('SECRET_SENTINEL'),'callback_server_disconnected',None),
                  (ConnectionResetError('SECRET_SENTINEL'),'callback_connection_reset',None),
                  (aiohttp.ClientPayloadError('SECRET_SENTINEL'),'callback_response_incomplete',None)]
        for failure,code,number in failures:
            transport=self.transport();session,context=fake_session(error=failure)
            with patch.object(transport,'_addresses',AsyncMock(return_value=[entry()])),patch.object(transport_module.aiohttp,'TCPConnector'),patch.object(transport_module.aiohttp,'ClientSession',return_value=context):
                with self.assertRaises(EventError) as caught:await transport.post(URL,b'{}',{})
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(transport.last_transport['attempts'][0]['errno'],number)
            self.assertNotIn('SECRET_SENTINEL',str(transport.last_transport))
    async def test_duplicate_addresses_and_stop_before_retry(self):
        transport=self.transport()
        async def once(*args):
            await transport.close();raise EventError('callback_proxy_failed',http_status=502)
        with patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            with self.assertRaisesRegex(EventError,'callback_stopping'):await transport.post(URL,b'{}',{})
        self.assertEqual(len(transport.last_transport['attempts']),1)

    async def test_dns_or_validation_failure_clears_prior_attempt_snapshot(self):
        transport=self.transport()
        transport.last_transport={'attempts':[{'error':'old'}]}
        with self.assertRaises(EventError):await transport.post('https://not-allowed.example/x',b'{}',{})
        self.assertEqual(transport.last_transport['attempts'],[])
        transport.last_transport={'attempts':[{'error':'old'}]}
        with patch.object(transport,'_addresses',AsyncMock(side_effect=EventError('callback_dns_failed'))):
            with self.assertRaises(EventError):await transport.post(URL,b'{}',{})
        self.assertEqual(transport.last_transport['attempts'],[])

    async def test_wrapped_tls_reset_classified_without_replay_or_secret(self):
        transport=self.transport()
        failure=aiohttp.ClientConnectorError(None,ConnectionResetError(errno.ECONNRESET,'SECRET_SENTINEL'))
        session,context=fake_session(error=failure)
        with patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport_module.aiohttp,'TCPConnector'),patch.object(transport_module.aiohttp,'ClientSession',return_value=context):
            with self.assertRaisesRegex(EventError,'callback_connection_reset'):
                await transport.post(URL,b'{}',{})
        self.assertEqual(session.post.call_count,1)
        attempt=transport.last_transport['attempts'][0]
        self.assertEqual(attempt['exception_category'],'connector_error')
        self.assertEqual(attempt['cause_category'],'connection_reset')
        self.assertEqual(attempt['errno'],'ECONNRESET')
        self.assertNotIn('SECRET_SENTINEL',str(transport.last_transport))

    async def test_first_candidate_gets_full_budget_and_second_only_remaining(self):
        transport=self.transport(); budgets=[]
        async def once(url,body,headers,entries,test,timeout,detail):
            budgets.append(timeout)
            if len(budgets)==1:
                await asyncio.sleep(.04)
                raise EventError('callback_proxy_failed',http_status=503)
            return 200,b'ok'
        with patch.object(transport_module,'NETWORK_TIMEOUT',.2),patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            await transport.post(URL,b'{}',{})
        self.assertGreater(budgets[0],.18)
        self.assertLess(budgets[1],budgets[0]-.03)
        self.assertGreater(transport.last_transport['attempts'][0]['elapsed_ms'],30)

    async def test_first_candidate_can_succeed_after_old_half_deadline(self):
        transport=self.transport()
        async def once(url,body,headers,entries,test,timeout,detail):
            async with asyncio.timeout(timeout):await asyncio.sleep(.12)
            return 200,b'ok'
        with patch.object(transport_module,'NETWORK_TIMEOUT',.2),patch.object(transport,'_addresses',AsyncMock(return_value=self.addresses())),patch.object(transport,'_post_once',once):
            self.assertEqual(await transport.post(URL,b'{}',{}),(200,b'ok'))
        self.assertEqual(len(transport.last_transport['attempts']),1)
