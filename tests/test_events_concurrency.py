"""Deterministic Events concurrency regressions; all callback I/O is synthetic."""
import asyncio
import base64
from collections import defaultdict, deque
import hashlib
import hmac
import importlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
events_module = importlib.import_module('astrbot_dot_plugin.events')
transport_module = importlib.import_module('astrbot_dot_plugin.callback_transport')
Events, EventError = events_module.Events, events_module.EventError
SECRET = 'whsec_' + base64.b64encode(b's' * 32).decode()
OTHER_SECRET = 'whsec_' + base64.b64encode(b't' * 32).decode()
URL_A = 'https://callbacks.example.com/a'
URL_B = 'https://callbacks.example.com/b'


def params(url, secret=SECRET):
    return {'name': 'qq.message', 'arguments': {}, 'ttlMs': None,
            'delivery': {'mode': 'webhook', 'url': url, 'secret': secret}}


def unsub(url):
    return {'name': 'qq.message', 'arguments': {},
            'delivery': {'mode': 'webhook', 'url': url}}


def mid(number):
    return 'msg_' + format(number, '032x')


class ControlledTransport(transport_module.CallbackTransport):
    """Exercise real POST/diagnostic logic, replacing DNS and HTTP with gates."""
    def __init__(self):
        super().__init__(['callbacks.example.com'])
        self.calls = []
        self.gates = {}
        self.started = {}
        self.statuses = defaultdict(deque)
        self.active = defaultdict(int)
        self.max_active = defaultdict(int)
        self.diagnostics = []
        self.db = None

    def block(self, url, kind='delivery'):
        key = url, kind
        self.gates[key] = asyncio.Event()
        self.started[key] = asyncio.Event()
        return self.gates[key]

    async def wait_started(self, url, kind='delivery'):
        await asyncio.wait_for(self.started[url, kind].wait(), 1)

    async def _system_addresses(self, host):
        return [{'hostname': host, 'host': '8.8.8.8', 'port': 443,
                 'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP,
                 'flags': socket.AI_NUMERICHOST}]

    async def _post_once(self, url, body, headers, entries, test, timeout, detail):
        if self.db is not None and self.db.in_transaction:
            raise AssertionError('network I/O inside a database transaction')
        value = json.loads(body)
        kind = 'verification' if value.get('type') == 'verification' else 'delivery'
        key = url, kind
        diagnostic = self.request_transport
        diagnostic['dns']['connections_created'] = 11 if url == URL_A else 22
        self.diagnostics.append((url, kind, diagnostic))
        self.calls.append((url, kind, body, dict(headers)))
        self.active[url] += 1
        self.max_active[url] = max(self.max_active[url], self.active[url])
        if key in self.started:
            self.started[key].set()
        try:
            if key in self.gates:
                await self.gates[key].wait()
            if self.request_transport is not diagnostic:
                raise AssertionError('another request replaced local diagnostics')
            if kind == 'verification':
                return 200, json.dumps({'challenge': value['challenge']}).encode()
            status = self.statuses[url].popleft() if self.statuses[url] else 204
            return status, b''
        finally:
            self.active[url] -= 1

    def deliveries(self, url):
        return [call for call in self.calls if call[:2] == (url, 'delivery')]


class EventsConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.events = Events(self.db, 'private-owner', ['callbacks.example.com'])
        await self.events.transport.close()
        self.transport = self.events.transport = ControlledTransport()
        self.transport.db = self.db
        self.now = int(time.time())
        self.events.clock = lambda: self.now
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.transport.close()
        self.db.close()

    def task(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    def enqueue(self, number):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue(mid(number), self.now)
        self.events.notify_enqueued()

    def ids(self, url):
        return [json.loads(call[2])['data']['message_id']
                for call in self.transport.deliveries(url)]

    def assert_signed(self, call, secret=SECRET):
        _, _, body, headers = call
        payload = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + body
        signature = 'v1,' + base64.b64encode(hmac.new(
            base64.b64decode(secret.removeprefix('whsec_')), payload, hashlib.sha256).digest()).decode()
        self.assertIn(signature, headers['webhook-signature'].split())
        if call[1] == 'delivery':
            self.assertEqual(json.loads(body)['eventId'], headers['webhook-id'])
            self.assertEqual(set(json.loads(body)['data']), {'message_id'})

    async def subscribe_both(self):
        await self.events.subscribe(params(URL_A))
        await self.events.subscribe(params(URL_B))

    async def test_slow_verification_does_not_block_another_subscription_delivery(self):
        await self.events.subscribe(params(URL_B))
        self.enqueue(1)
        gate = self.transport.block(URL_A, 'verification')
        slow = self.task(self.events.subscribe(params(URL_A)))
        await self.transport.wait_started(URL_A, 'verification')
        self.assertFalse(self.db.in_transaction)
        self.assertTrue(await asyncio.wait_for(self.events.deliver_once(), 1))
        self.assertEqual(self.ids(URL_B), [mid(1)])
        self.assertFalse(slow.done())
        gate.set()
        await slow
        self.assertEqual(self.events.status()['active_subscriptions'], 2)
        self.assertEqual(self.events._subscription_locks, {})

    async def test_slow_delivery_does_not_block_verification_of_another_subscription(self):
        await self.events.subscribe(params(URL_A))
        self.enqueue(1)
        gate = self.transport.block(URL_A)
        slow = self.task(self.events.deliver_once())
        await self.transport.wait_started(URL_A)
        await asyncio.wait_for(self.events.subscribe(params(URL_B)), 1)
        self.assertFalse(slow.done())
        self.assertEqual(self.events.status()['active_subscriptions'], 2)
        gate.set()
        self.assertTrue(await slow)

    async def test_concurrent_delivery_preserves_each_subscription_order_and_diagnostics(self):
        await self.subscribe_both()
        self.enqueue(1)
        self.enqueue(2)
        gate = self.transport.block(URL_A)
        slow = self.task(self.events.deliver_once())
        await self.transport.wait_started(URL_A)
        self.assertTrue(await asyncio.wait_for(self.events.deliver_once(), 1))
        self.assertTrue(await asyncio.wait_for(self.events.deliver_once(), 1))
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.ids(URL_A), [mid(1)])
        self.assertEqual(self.ids(URL_B), [mid(1), mid(2)])
        latest = self.transport.last_transport
        gate.set()
        self.assertTrue(await slow)
        self.assertIs(self.transport.last_transport, latest)
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.ids(URL_A), [mid(1), mid(2)])
        self.assertEqual(dict(self.transport.max_active), {URL_A: 1, URL_B: 1})
        samples = {sample['event_id']: sample for sample in
                   self.events.status()['delivery_latency']['recent']}
        for call in self.transport.calls:
            self.assert_signed(call)
            if call[1] == 'delivery':
                expected = 11 if call[0] == URL_A else 22
                self.assertEqual(samples[call[3]['webhook-id']]['dns_connections_created'], expected)
        self.assertEqual(self.events._subscription_locks, {})

    async def test_retry_head_blocks_only_its_subscription_and_keeps_exact_bytes(self):
        await self.subscribe_both()
        self.enqueue(1)
        self.enqueue(2)
        self.transport.statuses[URL_A].append(503)
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.events.deliver_once())
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.ids(URL_A), [mid(1)])
        self.assertEqual(self.ids(URL_B), [mid(1), mid(2)])
        self.now += 3
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.events.deliver_once())
        first, retry, _ = self.transport.deliveries(URL_A)
        self.assertEqual(first[2], retry[2])
        self.assertEqual(first[3]['webhook-id'], retry[3]['webhook-id'])
        self.assertNotEqual(first[3]['webhook-timestamp'], retry[3]['webhook-timestamp'])
        self.assertEqual(self.ids(URL_A), [mid(1), mid(1), mid(2)])
        self.assert_signed(first)
        self.assert_signed(retry)

    async def test_cancelled_inflight_lease_blocks_its_tail_but_not_another_subscription(self):
        await self.subscribe_both()
        self.enqueue(1)
        self.enqueue(2)
        gate = self.transport.block(URL_A)
        slow = self.task(self.events.deliver_once())
        await self.transport.wait_started(URL_A)
        slow.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await slow
        gate.set()
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.events.deliver_once())
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.ids(URL_A), [mid(1)])
        self.now += events_module.LEASE_SECONDS + 1
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.events.deliver_once())
        first, retry, _ = self.transport.deliveries(URL_A)
        self.assertEqual(first[2], retry[2])
        self.assertEqual(first[3]['webhook-id'], retry[3]['webhook-id'])
        self.assertEqual(self.ids(URL_A), [mid(1), mid(1), mid(2)])
        self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 4})
        self.assertEqual(self.events._subscription_locks, {})

    async def test_repeated_cancellation_exhausts_existing_attempt_bound(self):
        await self.events.subscribe(params(URL_A))
        self.enqueue(1)
        for _ in range(events_module.MAX_ATTEMPTS):
            self.transport.block(URL_A)
            slow = self.task(self.events.deliver_once())
            await self.transport.wait_started(URL_A)
            slow.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await slow
            self.now += events_module.LEASE_SECONDS + 1
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.db.execute('SELECT status,attempts FROM event_deliveries').fetchone(),
                         ('failed', events_module.MAX_ATTEMPTS))
        calls = self.transport.deliveries(URL_A)
        self.assertEqual(len(calls), events_module.MAX_ATTEMPTS)
        self.assertEqual(len({call[2] for call in calls}), 1)
        self.assertEqual(len({call[3]['webhook-id'] for call in calls}), 1)

    async def test_unsubscribe_waits_only_for_own_inflight_callback_and_cancels_tail(self):
        await self.subscribe_both()
        self.enqueue(1)
        self.enqueue(2)
        gate = self.transport.block(URL_A)
        slow = self.task(self.events.deliver_once())
        await self.transport.wait_started(URL_A)
        cancelled = self.task(self.events.unsubscribe(unsub(URL_A)))
        await asyncio.sleep(0)
        self.assertFalse(cancelled.done())
        self.assertTrue(await asyncio.wait_for(self.events.deliver_once(), 1))
        self.assertTrue(await asyncio.wait_for(self.events.deliver_once(), 1))
        gate.set()
        await slow
        self.assertEqual(await cancelled, {})
        self.assertFalse(await self.events.deliver_once())
        self.enqueue(3)
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.ids(URL_A), [mid(1)])
        self.assertEqual(self.ids(URL_B), [mid(1), mid(2), mid(3)])
        self.assertEqual(self.events._subscription_locks, {})
        row = self.db.execute("SELECT status,secret,old_secret FROM event_subscriptions WHERE url=?", (URL_A,)).fetchone()
        self.assertEqual(row, ('cancelled', '', None))

    async def test_unsubscribe_queued_behind_verification_cannot_be_revived(self):
        gate = self.transport.block(URL_A, 'verification')
        slow = self.task(self.events.subscribe(params(URL_A)))
        await self.transport.wait_started(URL_A, 'verification')
        cancelled = self.task(self.events.unsubscribe(unsub(URL_A)))
        await asyncio.sleep(0)
        await asyncio.wait_for(self.events.subscribe(params(URL_B)), 1)
        self.assertFalse(cancelled.done())
        gate.set()
        await slow
        await cancelled
        self.assertEqual(self.events.status()['active_subscriptions'], 1)
        self.assertEqual(self.db.execute('SELECT status FROM event_subscriptions WHERE url=?', (URL_A,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.events._subscription_locks, {})

    async def test_concurrent_new_verifications_recheck_active_limit_before_persist(self):
        for number in range(events_module.MAX_ACTIVE_SUBSCRIPTIONS - 1):
            await self.events.subscribe(params(URL_A + str(number)))
        gate_a = self.transport.block(URL_A, 'verification')
        gate_b = self.transport.block(URL_B, 'verification')
        first = self.task(self.events.subscribe(params(URL_A)))
        second = self.task(self.events.subscribe(params(URL_B)))
        await self.transport.wait_started(URL_A, 'verification')
        await self.transport.wait_started(URL_B, 'verification')
        gate_a.set()
        await first
        gate_b.set()
        with self.assertRaisesRegex(EventError, 'subscription_limit_reached'):
            await second
        self.assertEqual(self.events.status()['active_subscriptions'], events_module.MAX_ACTIVE_SUBSCRIPTIONS)
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_subscriptions WHERE url=?', (URL_B,)).fetchone()[0], 0)
        self.assertEqual(self.events._subscription_locks, {})

    async def test_rotation_waits_for_own_delivery_and_preserves_dual_signatures(self):
        await self.subscribe_both()
        self.enqueue(1)
        self.enqueue(2)
        gate = self.transport.block(URL_A)
        slow = self.task(self.events.deliver_once())
        await self.transport.wait_started(URL_A)
        rotation = self.task(self.events.subscribe(params(URL_A, OTHER_SECRET)))
        await asyncio.sleep(0)
        self.assertFalse(rotation.done())
        self.assertTrue(await self.events.deliver_once())
        gate.set()
        await slow
        await rotation
        self.assertTrue(await self.events.deliver_once())
        first, rotated = self.transport.deliveries(URL_A)
        self.assertEqual(len(first[3]['webhook-signature'].split()), 1)
        self.assertEqual(len(rotated[3]['webhook-signature'].split()), 2)
        self.assert_signed(rotated, SECRET)
        self.assert_signed(rotated, OTHER_SECRET)

    async def test_cancelled_lock_waiter_does_not_release_holder_or_leak_lock(self):
        sid = (await self.events.subscribe(params(URL_A)))['id']
        self.enqueue(1)
        async with self.events._subscription_lock(sid):
            waiting = self.task(self.events.unsubscribe(unsub(URL_A)))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertIn(sid, self.events._subscription_locks)
            self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.events._subscription_locks, {})
        self.assertTrue(self.events.delivery_wakeup.is_set())
        self.assertTrue(await self.events.deliver_once())


if __name__ == '__main__':
    unittest.main()
