"""Routed callback lifecycle tests with in-memory SQLite and fake transport.

No listener, tunnel, live account or external network is used.
"""
import asyncio
import json
import sqlite3
import types
import unittest

from multidict import CIMultiDict
from test_events import Events, EventError, FakeTransport, MID, subscribe_params, unsubscribe_params
from test_endpoint import Endpoint, TOKEN, endpoint_module


class GuardedTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.before_send = None
        self.after_send = None
        self.guards = []

    async def post(self, url, body, headers, *, preflight=None):
        self.guards.append(preflight)
        def check():
            try:
                allowed = preflight is None or preflight() is True
            except Exception:
                allowed = False
            if not allowed:
                raise EventError('callback_preflight_rejected')
        check()
        if self.before_send is not None:
            await self.before_send()
        check()
        result = await super().post(url, body, headers)
        if self.after_send is not None:
            self.after_send()
        return result


class RouteCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.events = Events(self.db, 'unchanged-owner', ['callbacks.example.com'])
        await self.events.transport.close()
        self.transport = GuardedTransport()
        self.events.transport = self.transport
        self.active = True
        self.events.route_active = lambda: self.active

    async def asyncTearDown(self):
        await self.events.transport.close()
        self.db.close()

    async def enqueue(self):
        await self.events.subscribe(subscribe_params())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue(MID, int(self.events.clock()))
        self.transport.calls.clear()
        self.transport.guards.clear()

    def row(self):
        return self.db.execute('SELECT status,attempts,next_attempt,error,lease_until FROM event_deliveries').fetchone()

    async def test_startup_barrier_preserves_queue_then_delivers_same_event(self):
        await self.enqueue()
        before = self.row()
        self.active = False
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.row(), before)
        self.assertEqual(self.transport.calls, [])
        self.active = True
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.row()[:2], ('acknowledged', 1))
        self.assertEqual(json.loads(self.transport.calls[0][1])['data']['message_id'], MID)
        self.assertTrue(callable(self.transport.guards[0]))

    async def test_revocation_during_transport_setup_does_not_consume_retry(self):
        await self.enqueue()
        before = self.row()
        async def revoke():
            await asyncio.sleep(0)
            self.active = False
        self.transport.before_send = revoke
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.row(), before)
        self.assertEqual(self.transport.calls, [])
        self.transport.before_send = None
        self.active = True
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.row()[:2], ('acknowledged', 1))

    async def test_last_retry_is_not_exhausted_by_route_barrier(self):
        await self.enqueue()
        with self.db:
            self.db.execute("UPDATE event_deliveries SET status='retry', attempts=4")
        before = self.row()
        async def revoke():
            self.active = False
        self.transport.before_send = revoke
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.row(), before)

    async def test_revoked_message_is_cancelled_without_blocking_next_message(self):
        await self.enqueue()
        other_mid = 'msg_' + '2' * 32
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue(other_mid, int(self.events.clock()))
        self.events.delivery_authorize = lambda mid: mid != MID
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.row()[:2], ('cancelled', 0))
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(json.loads(self.transport.calls[0][1])['data']['message_id'], other_mid)

    async def test_message_revocation_during_transport_setup_is_terminal(self):
        await self.enqueue()
        allowed = True
        self.events.delivery_authorize = lambda mid: allowed
        async def revoke():
            nonlocal allowed
            allowed = False
        self.transport.before_send = revoke
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.row()[:2], ('cancelled', 0))
        self.assertEqual(self.row()[3:], ('binding_inactive', None))
        self.assertEqual(self.transport.calls, [])

    async def test_inactive_route_never_cancels_temporarily_unreadable_message(self):
        await self.enqueue()
        self.active = False
        self.events.delivery_authorize = lambda mid: False
        before = self.row()
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.row(), before)

    async def test_unexpected_validator_failure_never_cancels_pending_message(self):
        await self.enqueue()
        before = self.row()
        def unavailable(mid):
            raise sqlite3.OperationalError('database is locked')
        self.events.delivery_authorize = unavailable
        with self.assertRaises(sqlite3.OperationalError):
            await self.events.deliver_once()
        self.assertEqual(self.row(), before)
        self.assertEqual(self.transport.calls, [])

    async def test_validator_failure_during_transport_preserves_retry(self):
        await self.enqueue()
        before = self.row()
        failed = False
        def validate(mid):
            if failed:
                raise sqlite3.OperationalError('database is locked')
            return True
        async def fail():
            nonlocal failed
            failed = True
        self.events.delivery_authorize = validate
        self.transport.before_send = fail
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.row(), before)
        self.assertEqual(self.transport.calls, [])

    async def test_inactive_subscribe_or_unsubscribe_never_contacts_or_changes(self):
        await self.events.subscribe(subscribe_params())
        before = self.db.execute('SELECT * FROM event_subscriptions').fetchall()
        self.transport.calls.clear()
        self.active = False
        for action, params in ((self.events.subscribe, subscribe_params()),
                               (self.events.unsubscribe, unsubscribe_params())):
            with self.assertRaisesRegex(EventError, 'binding_inactive'):
                await action(params)
        self.assertEqual(self.db.execute('SELECT * FROM event_subscriptions').fetchall(), before)
        self.assertEqual(self.transport.calls, [])

    async def test_verification_preflight_blocks_after_revocation(self):
        async def revoke():
            self.active = False
        self.transport.before_send = revoke
        with self.assertRaisesRegex(EventError, 'callback_preflight_rejected'):
            await self.events.subscribe(subscribe_params())
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_subscriptions').fetchone()[0], 0)

    async def test_completed_verification_cannot_activate_revoked_route(self):
        self.transport.after_send = lambda: setattr(self, 'active', False)
        with self.assertRaisesRegex(EventError, 'binding_inactive'):
            await self.events.subscribe(subscribe_params())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_subscriptions').fetchone()[0], 0)

    async def test_waiting_unsubscribe_rechecks_route_after_lock(self):
        result = await self.events.subscribe(subscribe_params())
        async with self.events._subscription_lock(result['id']):
            task = asyncio.create_task(self.events.unsubscribe(unsubscribe_params()))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.active = False
        with self.assertRaisesRegex(EventError, 'binding_inactive'):
            await task
        self.assertEqual(self.db.execute('SELECT status FROM event_subscriptions').fetchone()[0], 'active')

    async def test_broken_guard_fails_closed_without_queue_mutation(self):
        await self.enqueue()
        before = self.row()
        for provider in (False, lambda: 1, lambda: (_ for _ in ()).throw(ValueError('secret'))):
            self.events.route_active = provider
            self.assertFalse(await self.events.deliver_once())
            self.assertEqual(self.row(), before)


class RouteEndpointGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_guard_is_rechecked_after_request_body_and_before_tools(self):
        host = types.SimpleNamespace(route_active=lambda: False)
        endpoint = Endpoint(host, TOKEN)
        for method in ('server/discover', 'events/subscribe', 'events/unsubscribe', 'tools/call'):
            with self.assertRaises(endpoint_module._RPCError) as result:
                await endpoint._dispatch(method, {})
            self.assertEqual(result.exception.reason, 'binding_inactive')
        with self.assertRaises(endpoint_module._RPCError):
            await endpoint._tool({})

    async def test_route_http_unavailable_only_after_authentication(self):
        host = types.SimpleNamespace(route_active=lambda: False)
        endpoint = Endpoint(host, TOKEN)
        endpoint._stopping = False
        request = types.SimpleNamespace(headers=CIMultiDict({'Authorization': 'Bearer ' + TOKEN}))
        self.assertEqual((await endpoint._handle(request)).status, 503)
        request.headers['Authorization'] = 'wrong'
        self.assertEqual((await endpoint._handle(request)).status, 401)

    async def test_nonroute_discovery_behavior_is_unchanged(self):
        endpoint = Endpoint(types.SimpleNamespace(), TOKEN)
        result = await endpoint._dispatch('server/discover', {})
        self.assertIn('supportedVersions', result)


if __name__ == '__main__':
    unittest.main()
