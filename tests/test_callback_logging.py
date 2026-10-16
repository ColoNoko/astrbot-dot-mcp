"""Native logger wiring with real endpoint/Events and offline callback I/O."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from test_events import EventError, events_module, subscribe_params
from test_endpoint import endpoint_module
from test_main import load_main


class RecordingLogger:
    def __init__(self):
        self.records = []

    def error(self, value):
        self.records.append(('error', value))

    def warning(self, value):
        self.records.append(('warning', value))

    def info(self, value):
        self.records.append(('info', value))


class CallbackLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = RecordingLogger()
        # Keep AstrBot API-shaped fixtures, but reload Main with the real
        # Endpoint module and the logger imported from astrbot.api itself.
        modules = patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        load_main(self.tmp.name)
        sys.modules['astrbot.api'].logger = self.logger
        path = Path(__file__).resolve().parents[1] / 'main.py'
        spec = importlib.util.spec_from_file_location('astrbot_dot_plugin.logging_main', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.token = 'MCP_TOKEN_SENTINEL_' + 'x' * 32
        self.plugin = self.module.Main(object(), {
            'enabled': True, 'platform_id': 'instance-1', 'adapter': 'aiocqhttp',
            'self_id': '100', 'sender_id': '200', 'mcp_token': self.token,
            'callback_hosts': ['callbacks.example.com'], 'listen_port': 0,
        })
        await self.plugin.initialize()
        self.assertTrue(self.plugin.ready)
        self.assertIsInstance(self.plugin.endpoint, endpoint_module.Endpoint)
        self.events = self.plugin.endpoint.events
        self.assertIsInstance(self.events, events_module.Events)
        self.assertIs(self.module.logger, self.logger)
        self.assertIs(self.plugin.endpoint.logger, self.logger)
        self.assertIs(self.events.logger, self.logger)
        self.transport = self.events.transport
        self.transport._addresses = AsyncMock(return_value=[{
            'hostname': 'callbacks.example.com', 'host': '8.8.8.8', 'port': 443,
            'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP, 'flags': socket.AI_NUMERICHOST,
        }])
        self.transport._post_once = AsyncMock(side_effect=self.fail_tls)
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def rpc(self, params=None):
        url = 'http://127.0.0.1:%s/mcp' % self.plugin.endpoint.port
        async with self.client.post(url, headers={
                'Authorization': 'Bearer ' + self.token,
                'MCP-Protocol-Version': endpoint_module.PROTOCOL_VERSION,
                'Mcp-Method': 'events/subscribe'}, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': 'events/subscribe',
                    'params': subscribe_params() if params is None else params}) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def fail_tls(self, url, body, headers, entries, test, timeout, detail):
        detail.update(phase='tls', connect_status=200, response_status=None,
                      tls_category='certificate_verification', tls_verify_code=20,
                      tls_reason='certificate_verify_failed')
        raise EventError('callback_tls_failed')

    async def echo(self, url, body, headers, entries, test, timeout, detail):
        detail.update(phase='response_body', response_status=200)
        return 200, json.dumps({'challenge': json.loads(body)['challenge']}).encode()

    def allow_next_log(self):
        self.events._last_subscription_log_at -= events_module.SUBSCRIPTION_LOG_INTERVAL

    async def test_native_initialize_real_endpoint_failure_emits_warning(self):
        result = await self.rpc()
        self.assertEqual(result['error']['code'], -32015)
        self.assertEqual(result['error']['data']['reason'], 'callback_tls_failed')
        self.assertEqual(len(self.logger.records), 1)
        level, line = self.logger.records[0]
        self.assertEqual(level, 'warning')
        for value in ('dot_private_subscription outcome=failure',
                      'stage=callback_verification', 'error=callback_tls_failed',
                      'phase=tls', 'connect_status=200', 'response_status=none',
                      'tls_category=certificate_verification', 'tls_verify_code=20',
                      'tls_reason=certificate_verify_failed'):
            self.assertIn(value, line)
        self.assertRegex(line, r'elapsed_ms=\d+ ')
        for value in (self.token, subscribe_params()['delivery']['secret'],
                      'callbacks.example.com', 'https://', '/events', 'challenge'):
            self.assertNotIn(value, line)

    async def test_success_logs_info_and_cached_refresh_has_no_stale_attempt(self):
        self.transport._post_once.side_effect = self.echo
        self.assertIn('result', await self.rpc())
        self.assertEqual(self.logger.records[0][0], 'info')
        self.assertIn('outcome=success stage=active error=none phase=response_body',
                      self.logger.records[0][1])
        self.assertIn('response_status=200', self.logger.records[0][1])
        self.allow_next_log()
        self.assertIn('result', await self.rpc())
        self.assertEqual(self.transport._post_once.await_count, 1)
        self.assertIn('phase=none elapsed_ms=none connect_status=none response_status=none',
                      self.logger.records[1][1])

    async def test_rate_limit_is_shared_across_attempts_and_outcomes(self):
        for _ in range(12):
            self.assertIn('error', await self.rpc())
        self.transport._post_once.side_effect = self.echo
        self.assertIn('result', await self.rpc())
        self.assertEqual(len(self.logger.records), 1)
        self.assertEqual(self.events.status()['last_subscribe']['attempts'], 13)
        self.allow_next_log()
        self.assertIn('result', await self.rpc())
        self.assertEqual([item[0] for item in self.logger.records], ['warning', 'info'])

    async def test_validation_failure_never_reuses_previous_tls_details(self):
        await self.rpc()
        self.allow_next_log()
        params = subscribe_params(secret='SIGNING_SECRET_SENTINEL')
        self.assertIn('error', await self.rpc(params))
        line = self.logger.records[-1][1]
        self.assertIn('stage=validate_request error=invalid_signing_secret phase=none', line)
        self.assertNotIn('tls_category=', line)
        self.assertNotIn('SIGNING_SECRET_SENTINEL', line)
        self.assertEqual(self.transport._post_once.await_count, 1)

    async def test_unknown_malicious_diagnostics_are_never_formatted(self):
        sentinel = 'SECRET_SENTINEL https://private.example/key\nforged-log'
        async def poisoned(url, body, headers, entries, test, timeout, detail):
            detail.update(phase=sentinel, elapsed_ms=sentinel, connect_status=True,
                          response_status=sentinel, tls_category=[sentinel],
                          tls_verify_code=sentinel, tls_reason={'secret': sentinel},
                          url=url, headers=headers, response_body=sentinel,
                          exception_category=sentinel, cause_category=sentinel)
            raise EventError(sentinel)
        self.transport._post_once.side_effect = poisoned
        self.assertIn('error', await self.rpc())
        line = self.logger.records[-1][1]
        self.assertIn('error=event_operation_failed phase=none', line)
        self.assertIn('connect_status=none response_status=none', line)
        self.assertIn('tls_category=none tls_verify_code=none tls_reason=none', line)
        self.assertNotIn('SECRET_SENTINEL', line)
        self.assertNotIn('\n', line)
        self.assertLess(len(line), 650)

    async def test_formatter_revalidates_enum_numeric_types_and_bounds(self):
        class Poison:
            def __str__(self):
                raise AssertionError('Raw diagnostic object was formatted')
        for bad in (True, -1, 2147483648, float('inf'), float('nan'), '200', Poison()):
            self.events._last_subscription_log_at = None
            self.events._log_subscription(Poison(), Poison(), {'attempts': [{
                'phase': Poison(), 'elapsed_ms': bad, 'connect_status': bad,
                'response_status': bad, 'tls_category': Poison(),
                'tls_verify_code': bad, 'tls_reason': Poison()}]})
            line = self.logger.records[-1][1]
            self.assertIn('stage=unknown error=event_operation_failed phase=none', line)
            self.assertIn('elapsed_ms=none connect_status=none response_status=none', line)
            self.assertIn('tls_verify_code=none', line)
        self.assertEqual(len(self.logger.records), 7)

    async def test_dns_failure_logs_before_any_callback_connection_attempt(self):
        async def dns_failure(_host):
            self.transport.last_transport['dns'] = {
                'mode': 'google', 'phase': 'response_body', 'error': 'http_status',
                'elapsed_ms': 27, 'http_status': 503, 'connect_status': 200}
            raise EventError('callback_dns_failed')
        self.transport._addresses.side_effect = dns_failure
        result = await self.rpc()
        self.assertEqual(result['error']['data']['reason'], 'callback_dns_failed')
        self.transport._post_once.assert_not_awaited()
        self.assertEqual(self.transport.last_transport['attempts'], [])
        level, line = self.logger.records[-1]
        self.assertEqual(level, 'warning')
        for text in ('error=callback_dns_failed phase=none', 'dns_mode=google',
                     'dns_phase=response_body', 'dns_error=http_status',
                     'dns_elapsed_ms=27', 'dns_http_status=503', 'dns_connect_status=200'):
            self.assertIn(text, line)
        for text in ('callbacks.example.com', 'https://', self.token,
                     subscribe_params()['delivery']['secret']):
            self.assertNotIn(text, line)

    async def test_dns_log_revalidates_each_field_without_formatting_arbitrary_values(self):
        class Poison:
            def __str__(self):
                raise AssertionError('Raw DNS diagnostic object was formatted')
        for bad in (True, -1, 2147483648, float('inf'), 'SECRET_DNS_SENTINEL', Poison()):
            self.events._last_subscription_log_at = None
            self.events._log_subscription('callback_verification', 'callback_dns_failed', {
                'attempts': [], 'dns': dict.fromkeys(
                    ('mode', 'phase', 'error', 'elapsed_ms', 'http_status', 'connect_status'), bad)})
            line = self.logger.records[-1][1]
            self.assertIn('dns_mode=none dns_phase=none dns_error=none', line)
            self.assertIn('dns_elapsed_ms=none dns_http_status=none dns_connect_status=none', line)
            self.assertNotIn('SECRET_DNS_SENTINEL', line)
        self.assertEqual(len(self.logger.records), 6)

    async def test_logger_exceptions_do_not_change_success_or_failure(self):
        def broken(_line):
            raise RuntimeError('LOGGER_SECRET_SENTINEL')
        self.logger.warning = self.logger.info = broken
        result = await self.rpc()
        self.assertEqual(result['error']['data']['reason'], 'callback_tls_failed')
        self.allow_next_log()
        self.transport._post_once.side_effect = self.echo
        self.assertIn('result', await self.rpc())
        self.assertEqual(self.events.status()['active_subscriptions'], 1)

    async def test_cancellation_logs_current_attempt_and_still_cancels(self):
        started = asyncio.Event()
        async def blocked(url, body, headers, entries, test, timeout, detail):
            detail.update(phase='connect_response', connect_status=200)
            started.set()
            await asyncio.Event().wait()
        self.transport._post_once.side_effect = blocked
        task = asyncio.create_task(self.events.subscribe(subscribe_params()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('error=subscription_cancelled phase=connect_response',
                      self.logger.records[-1][1])


if __name__ == '__main__':
    unittest.main()
