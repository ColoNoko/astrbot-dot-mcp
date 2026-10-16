import asyncio
import json
import sqlite3
import unittest
from test_events import Events, FakeTransport, subscribe_params, SECRET
from astrbot_dot_plugin.event_errors import EventError

class SubscriptionDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=sqlite3.connect(':memory:'); self.events=Events(self.db,'owner',['callbacks.example.com'])
        await self.events.transport.close(); self.fake=FakeTransport(); self.events.transport=self.fake
    async def asyncTearDown(self):
        self.db.close()
    async def test_initial_state_and_failed_validation_without_sensitive_values(self):
        self.assertIsNone(self.events.status()['last_subscribe'])
        params=subscribe_params(); params['delivery']['secret']='TOKEN_SENTINEL'
        with self.assertRaises(EventError): await self.events.subscribe(params)
        status=self.events.status(); diagnostic=status['last_subscribe']
        self.assertEqual(diagnostic['stage'],'validate_request')
        self.assertEqual(diagnostic['error'],'invalid_signing_secret')
        self.assertNotIn('TOKEN_SENTINEL',str(status)); self.assertNotIn('https://',str(status))
    async def test_dns_failure_and_restart_retained(self):
        self.fake.error='callback_dns_failed'
        with self.assertRaises(EventError): await self.events.subscribe(subscribe_params())
        other=Events(self.db,'owner',['callbacks.example.com'])
        self.assertEqual(other.status()['last_subscribe']['error'],'callback_dns_failed')
        self.assertEqual(other.status()['last_subscribe']['stage'],'callback_verification')
        await other.transport.close()
    async def test_http_status_and_success_clear_error(self):
        async def reject(*args): return 403,b'RAW_RESPONSE_SECRET'
        self.fake.post=reject
        with self.assertRaises(EventError): await self.events.subscribe(subscribe_params())
        diag=self.events.status()['last_subscribe']
        self.assertEqual(diag['http_status'],403); self.assertEqual(diag['error'],'challenge_failed')
        self.assertNotIn('RAW_RESPONSE_SECRET',str(diag))
        self.events.transport=FakeTransport()
        await self.events.subscribe(subscribe_params())
        diag=self.events.status()['last_subscribe']
        self.assertEqual(diag['attempts'],2); self.assertEqual(diag['stage'],'active'); self.assertIsNone(diag['error'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event_subscription_diagnostics').fetchone()[0],1)
    async def test_unknown_error_and_proxy_status_are_safe(self):
        async def fail(*args): raise EventError('unknown-secret',http_status=407)
        self.fake.post=fail
        with self.assertRaises(EventError): await self.events.subscribe(subscribe_params())
        diag=self.events.status()['last_subscribe']
        self.assertEqual(diag['error'],'event_operation_failed'); self.assertEqual(diag['http_status'],407)
        self.assertNotIn('unknown-secret',str(diag))
    async def test_cancelled_attempt_recorded(self):
        self.fake.block=asyncio.Event()
        task=asyncio.create_task(self.events.subscribe(subscribe_params()))
        await self.fake.started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.events.status()['last_subscribe']['error'],'subscription_cancelled')
    async def test_older_attempt_cannot_overwrite_new_attempt(self):
        first=self.events._diagnostic_start(); second=self.events._diagnostic_start()
        self.events._diagnostic_update(first,'active')
        self.assertEqual(self.events.status()['last_subscribe']['stage'],'validate_request')
        self.assertEqual(self.events.status()['last_subscribe']['attempts'],second)

if __name__=='__main__': unittest.main()
