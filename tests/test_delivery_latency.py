"""Offline bounded telemetry and post-commit wake regressions; no QQ/network callbacks."""
import asyncio
import base64
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
events_module = importlib.import_module('astrbot_dot_plugin.events')
endpoint_module = importlib.import_module('astrbot_dot_plugin.endpoint')
host_module = importlib.import_module('astrbot_dot_plugin.host')
SECRET = 'whsec_' + base64.b64encode(b's' * 32).decode()
URL = 'https://callbacks.example.com/events'
PARAMS = {'name': 'qq.message', 'arguments': {}, 'ttlMs': None,
          'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}}


def mid(number=1):
    return 'msg_' + format(number, '032x')


class TimingTransport:
    callback_dns = 'google'
    backend = 'aiohttp'

    def __init__(self):
        self.status = 204
        self.last_transport = None
        self.started = asyncio.Event()
        self.block = None
        self.deliveries = []

    def validate(self, url):
        pass

    async def post(self, url, body, headers):
        self.last_transport = {'dns': {'elapsed_ms': 7, 'connection_reused': True,
            'connections_created': 1, 'connections_reused': 1},
            'attempts': [{'elapsed_ms': 11}, {'elapsed_ms': 13}]}
        value = json.loads(body)
        if value.get('type') == 'verification':
            return 200, json.dumps({'challenge': value['challenge']}).encode()
        self.deliveries.append((body, headers))
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        return self.status, b''

    async def close(self):
        pass


class DeliveryLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:')
        self.events = events_module.Events(self.db, 'test-owner')
        await self.events.transport.close()
        self.transport = self.events.transport = TimingTransport()
        await self.events.subscribe(PARAMS)

    async def asyncTearDown(self):
        await self.events.transport.close()
        self.db.close()

    def enqueue(self, number=1, *, capture=True):
        stamp = self.events._latency_stamp() if capture else None
        with self.db:
            # Explicit BEGIN is required before enqueue if this is the first write.
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue(mid(number), int(time.time()), captured_at=stamp, ingest_at=stamp)

    def sample(self):
        return self.events.status()['delivery_latency']['recent'][-1]

    async def test_measured_queue_age_lock_wait_and_actual_utc_milestones(self):
        self.enqueue()
        await asyncio.sleep(0.02)
        sid, _, _ = self.events._identity(PARAMS, True)
        async with self.events._subscription_lock(sid):
            # Busy subscriptions are skipped instead of monopolizing a worker.
            self.assertFalse(await self.events.deliver_once())
            await asyncio.sleep(0.03)
        self.assertTrue(await self.events.deliver_once())
        sample = self.sample()
        self.assertEqual(sample['message_id'], mid())
        self.assertEqual(sample['outcome'], 'acknowledged')
        durations = sample['durations_ms']
        self.assertGreaterEqual(durations['queue_to_claim'], 40)
        self.assertGreaterEqual(durations['callback_lock_wait'], 0)
        self.assertLessEqual(durations['callback_lock_wait'], durations['queue_to_claim'])
        self.assertGreaterEqual(durations['capture_to_callback_ack'], durations['queue_to_claim'])
        self.assertEqual(durations['dns'], 7)
        self.assertEqual(durations['callback_attempts'], [11, 13])
        self.assertIs(sample['dns_connection_reused'], True)
        # Reuse and reconnect may both occur during one DNS operation.
        self.assertEqual(sample['dns_connections_created'], 1)
        now_utc = time.time_ns() // 1_000_000
        now_mono = time.monotonic_ns() // 1_000_000
        for stamp in sample['timestamps'].values():
            if stamp is None:
                continue
            self.assertLessEqual(stamp['utc_ms'], now_utc)
            self.assertLessEqual(stamp['monotonic_ms'], now_mono)
        self.assertEqual(sample['timestamps']['callback_ack'], sample['timestamps']['transport_finished'])
        rendered = json.dumps(self.events.status())
        for private in (SECRET, URL, 'test-owner'):
            self.assertNotIn(private, rendered)

    async def test_clock_jump_does_not_inflate_measured_monotonic_queue_time(self):
        self.enqueue()
        self.events.clock = lambda: time.time() + 3600
        await self.events.deliver_once()
        sample = self.sample()
        self.assertGreaterEqual(sample['pending_queue_wall_ms'], 3_600_000)
        self.assertLess(sample['durations_ms']['queue_to_claim'], 1000)
        self.assertLessEqual(sample['timestamps']['callback_ack']['utc_ms'], time.time_ns() // 1_000_000)

    async def test_restart_has_unknown_capture_and_monotonic_queue_time(self):
        self.enqueue()
        await asyncio.sleep(0.01)
        await self.events.transport.close()
        self.events = events_module.Events(self.db, 'test-owner')
        await self.events.transport.close()
        self.events.transport = self.transport
        self.assertTrue(await self.events.deliver_once())
        sample = self.sample()
        for key in ('captured', 'ingest_started', 'enqueued'):
            self.assertIsNone(sample['timestamps'][key])
        for key in ('queue_to_claim', 'capture_to_callback_ack', 'enqueue_to_callback_ack'):
            self.assertIsNone(sample['durations_ms'][key])
        self.assertGreaterEqual(sample['pending_queue_wall_ms'], 5)

    async def test_origins_and_attempt_ring_are_bounded_and_eviction_is_honest(self):
        limit = events_module.MAX_DELIVERY_LATENCY_SAMPLES
        self.assertLessEqual(limit, 32)
        for number in range(1, limit + 9):
            self.enqueue(number)
        self.assertEqual(len(self.events._delivery_origins), limit)
        await self.events.deliver_once()
        self.assertIsNone(self.sample()['timestamps']['enqueued'])
        for _ in range(limit + 7):
            self.assertTrue(await self.events.deliver_once())
        status = self.events.status()['delivery_latency']
        self.assertEqual(status['retained'], limit)
        self.assertEqual(len(status['recent']), limit)
        self.assertEqual(status['recent'][0]['message_id'], mid(9))
        self.assertEqual(status['recent'][-1]['message_id'], mid(limit + 8))
        self.assertEqual(len(self.events._delivery_origins), 0)
        self.assertEqual(status['summary_ms']['queue_to_claim']['count'], limit)

    async def test_transport_and_status_snapshots_cannot_mutate_old_samples(self):
        self.enqueue()
        await self.events.deliver_once()
        self.transport.last_transport['dns']['elapsed_ms'] = 999
        self.transport.last_transport['attempts'][0]['elapsed_ms'] = 999
        copy = self.events.status()['delivery_latency']
        copy['recent'][-1]['durations_ms']['callback_attempts'][0] = 888
        copy['recent'][-1]['timestamps']['callback_ack']['utc_ms'] = 0
        sample = self.sample()
        self.assertEqual(sample['durations_ms']['dns'], 7)
        self.assertEqual(sample['durations_ms']['callback_attempts'], [11, 13])
        self.assertGreater(sample['timestamps']['callback_ack']['utc_ms'], 0)

    async def test_failed_post_cannot_inherit_subscription_transport_timing(self):
        self.enqueue()
        async def fail_without_new_diagnostics(*args):
            raise events_module.EventError('callback_timeout')
        self.transport.post = fail_without_new_diagnostics
        await self.events.deliver_once()
        sample = self.sample()
        self.assertEqual(sample['outcome'], 'retry')
        self.assertIsNone(sample['durations_ms']['dns'])
        self.assertEqual(sample['durations_ms']['callback_attempts'], [])
        self.assertIsNone(sample['timestamps']['callback_ack'])
        self.assertIsNone(sample['durations_ms']['capture_to_callback_ack'])
        self.assertFalse(await self.events.deliver_once())
        self.assertGreater(self.db.execute('SELECT next_attempt FROM event_deliveries').fetchone()[0], time.time())
        self.assertLessEqual(sample['timestamps']['transport_finished']['utc_ms'], time.time_ns() // 1_000_000)

    async def test_cancellation_reports_interrupted_and_retains_safe_lease_recovery(self):
        self.enqueue()
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.events.deliver_once())
        await self.transport.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        sample = self.sample()
        self.assertEqual(sample['outcome'], 'interrupted')
        self.assertIsNone(sample['timestamps']['callback_ack'])
        first = self.transport.deliveries[-1]
        self.events.clock = lambda: time.time() + events_module.LEASE_SECONDS + 1
        self.transport.block = None
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(first[0], self.transport.deliveries[-1][0])
        self.assertEqual(first[1]['webhook-id'], self.transport.deliveries[-1][1]['webhook-id'])
        self.assertEqual(self.sample()['attempt'], 2)

    async def test_snapshot_whitelists_ids_and_only_bounded_numeric_transport_fields(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue('private_identifier_sentinel', int(time.time()))
        original = self.transport.post
        async def malformed(*args):
            result = await original(*args)
            self.transport.last_transport = {'dns': {'elapsed_ms': 'private_secret',
                'connection_reused': 'private_url', 'connections_created': True,
                'connections_reused': -1}, 'attempts': [{'elapsed_ms': 2**40},
                {'elapsed_ms': False}, {'elapsed_ms': 4}], 'secret': 'private_secret'}
            return result
        self.transport.post = malformed
        await self.events.deliver_once()
        sample = self.sample()
        self.assertIsNone(sample['message_id'])
        self.assertIsNone(sample['durations_ms']['dns'])
        self.assertEqual(sample['durations_ms']['callback_attempts'], [None, None])
        for field in ('dns_connection_reused', 'dns_connections_created', 'dns_connections_reused'):
            self.assertIsNone(sample[field])
        rendered = json.dumps(sample)
        for private in ('private_identifier_sentinel', 'private_secret', 'private_url'):
            self.assertNotIn(private, rendered)

    async def test_real_transport_stage_timestamps_are_copied_without_network(self):
        self.enqueue()
        transport_module = importlib.import_module('astrbot_dot_plugin.callback_transport')
        real = transport_module.CallbackTransport(['callbacks.example.com'])
        async def offline_addresses(host):
            await asyncio.sleep(0.002)
            return []
        async def offline_post(*args):
            await asyncio.sleep(0.002)
            return 204, b''
        real._system_addresses = offline_addresses
        real._post_once = offline_post
        self.events.transport = real
        self.assertTrue(await self.events.deliver_once())
        sample = self.sample()
        dns = real.last_transport['dns']
        attempt = real.last_transport['attempts'][0]
        for target, source, prefix in (
                (sample['timestamps']['dns_started'], dns, 'started'),
                (sample['timestamps']['dns_completed'], dns, 'completed'),
                (sample['callback_attempt_timestamps'][0]['started'], attempt, 'started'),
                (sample['callback_attempt_timestamps'][0]['completed'], attempt, 'completed')):
            self.assertEqual(target['utc_ms'], source[prefix + '_utc_ms'])
            self.assertEqual(target['monotonic_ms'], source[prefix + '_monotonic_ms'])
            self.assertLessEqual(target['utc_ms'], time.time_ns() // 1_000_000)
        self.assertLessEqual(sample['timestamps']['transport_started']['monotonic_ms'],
                             sample['timestamps']['dns_started']['monotonic_ms'])
        self.assertLessEqual(sample['timestamps']['dns_completed']['monotonic_ms'],
                             sample['callback_attempt_timestamps'][0]['started']['monotonic_ms'])
        self.assertLessEqual(sample['callback_attempt_timestamps'][0]['completed']['monotonic_ms'],
                             sample['timestamps']['callback_ack']['monotonic_ms'])
        dns['started_utc_ms'] = 0
        attempt['started_monotonic_ms'] = 0
        self.assertGreater(self.sample()['timestamps']['dns_started']['utc_ms'], 0)
        self.assertGreater(self.sample()['callback_attempt_timestamps'][0]['started']['monotonic_ms'], 0)

    async def test_wall_age_is_bounded_and_malformed_due_time_is_unknown(self):
        age = self.events._latency_wall_age
        self.assertEqual(age(100, -1e308), 2147483647)
        self.assertEqual(age(1e308, -1e308), 2147483647)
        self.assertEqual(age(100, 200), 0)
        for due in (float('nan'), float('inf'), 'secret', True, None):
            self.assertIsNone(age(100, due))

    async def test_future_or_malformed_capture_stamps_remain_unknown(self):
        now = self.events._latency_stamp()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.events.enqueue(mid(), int(time.time()), captured_at=(now[0] + 100000, now[1] + 100000),
                                ingest_at=('secret', 1))
        await self.events.deliver_once()
        self.assertIsNone(self.sample()['timestamps']['captured'])
        self.assertIsNone(self.sample()['timestamps']['ingest_started'])


class DeliveryWakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = host_module.Store(Path(self.temp.name) / 'messages.db',
            host_module.Binding('platform', 'adapter', '100', '200'))
        self.host = types.SimpleNamespace(store=self.store)
        self.endpoint = endpoint_module.Endpoint(self.host, 'test-token-' + 'x' * 32, port=0)
        self.poll_patch = patch.object(endpoint_module, 'POLL_SECONDS', 5)
        self.poll_patch.start()
        await self.endpoint.start()
        self.events = self.endpoint.events
        await self.events.transport.close()
        self.transport = self.events.transport = TimingTransport()
        await self.events.subscribe(PARAMS)

    async def asyncTearDown(self):
        await self.endpoint.close()
        self.store.close()
        self.temp.cleanup()
        self.poll_patch.stop()

    def ingest(self, number=1):
        return self.store.ingest({'source_id': str(number), 'umo': 'private-origin',
                                 'text': 'private-message'}, captured_at=self.events._latency_stamp())

    async def wait_empty(self):
        empty = asyncio.Event()
        original = self.events.deliver_once
        async def observed():
            result = await original()
            if not result:
                empty.set()
            return result
        self.events.deliver_once = observed
        self.events.notify_enqueued()
        await asyncio.wait_for(empty.wait(), 0.3)
        await asyncio.sleep(0)

    async def test_post_commit_wakeup_beats_idle_timeout_and_dedupes(self):
        await self.wait_empty()
        observations = []
        original = self.events.notify_enqueued
        def committed():
            observations.append(self.store.db.in_transaction)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM event_deliveries').fetchone()[0], 1)
            original()
        self.events.notify_enqueued = committed
        message_id, fresh = self.ingest()
        self.assertTrue(fresh)
        self.assertEqual(self.ingest(), (message_id, False))
        await asyncio.wait_for(self.transport.started.wait(), 0.3)
        self.assertEqual(observations, [False])
        self.assertEqual(len(self.transport.deliveries), 1)
        sample = self.events.status()['delivery_latency']['recent'][-1]
        self.assertIsNotNone(sample['timestamps']['ingest_started'])
        self.assertLessEqual(sample['timestamps']['captured']['monotonic_ms'],
                             sample['timestamps']['ingest_started']['monotonic_ms'])

    async def test_enqueue_immediately_after_empty_read_does_not_lose_wakeup(self):
        original = self.events.deliver_once
        injected = False
        async def arrival_after_empty_read():
            nonlocal injected
            result = await original()
            if not result and not injected:
                injected = True
                self.ingest()
            return result
        self.events.deliver_once = arrival_after_empty_read
        self.events.notify_enqueued()
        await asyncio.wait_for(self.transport.started.wait(), 0.3)
        self.assertTrue(injected)
        self.assertEqual(len(self.transport.deliveries), 1)

    async def test_rollback_never_notifies_and_does_not_create_a_sample(self):
        await self.wait_empty()
        self.events.delivery_wakeup.clear()
        original = self.events.enqueue
        def fail_after_enqueue(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('synthetic failure')
        with patch.object(self.events, 'enqueue', fail_after_enqueue):
            with self.assertRaises(host_module.Rejected):
                self.ingest()
        self.assertFalse(self.events.delivery_wakeup.is_set())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM event_deliveries').fetchone()[0], 0)
        self.assertEqual(self.events.status()['delivery_latency']['recent'], [])

    async def test_notification_inside_transaction_is_ignored(self):
        await self.wait_empty()
        self.events.delivery_wakeup.clear()
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            self.events.notify_enqueued()
            self.assertFalse(self.events.delivery_wakeup.is_set())
        self.events.notify_enqueued()
        self.assertTrue(self.events.delivery_wakeup.is_set())

    async def test_close_and_reload_clean_pending_wait_and_process_persisted_queue(self):
        await self.wait_empty()
        previous_task = self.endpoint._delivery_task
        previous_wakeup = self.events.delivery_wakeup
        await self.endpoint.close()
        self.assertTrue(previous_task.done())
        self.assertFalse(previous_wakeup._waiters)
        self.assertIsNone(self.store.events)
        # A committed queue from before restart has no in-memory wake in the new Events.
        self.store.events = self.events
        self.ingest()
        self.store.events = None
        await self.endpoint.start()
        self.events = self.endpoint.events
        await self.events.transport.close()
        self.transport = self.events.transport = TimingTransport()
        await asyncio.wait_for(self.transport.started.wait(), 0.3)
        self.assertEqual(len(self.transport.deliveries), 1)
        self.assertIsNot(self.endpoint._delivery_task, previous_task)
        self.assertIsNone(self.events.status()['delivery_latency']['recent'][-1]['timestamps']['captured'])

    async def test_retry_is_not_busy_and_same_subscription_tail_waits_for_backoff(self):
        # A shortened fallback keeps the test quick without altering retry deadlines.
        with patch.object(endpoint_module, 'POLL_SECONDS', 0.02):
            now = time.time()
            self.events.clock = lambda: now
            calls = 0
            original = self.events.deliver_once
            async def counted():
                nonlocal calls
                calls += 1
                return await original()
            self.events.deliver_once = counted
            self.transport.status = 503
            self.ingest()
            await asyncio.wait_for(self.transport.started.wait(), 0.3)
            await asyncio.sleep(0.09)
            self.assertEqual(len(self.transport.deliveries), 1)
            self.assertLess(calls, 15)
            row = self.store.db.execute('SELECT next_attempt,attempts FROM event_deliveries').fetchone()
            self.assertEqual(tuple(row), (now + 2, 1))
            self.transport.started.clear()
            self.transport.status = 204
            second_mid, _ = self.ingest(2)
            await asyncio.sleep(0.04)
            self.assertFalse(self.transport.started.is_set())
            self.assertEqual(len(self.transport.deliveries), 1)
            now += 2
            # No enqueue signal: due retries must still be serviced by the fallback.
            async def drained():
                while len(self.transport.deliveries) < 3:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(drained(), 0.3)
            bodies = [json.loads(body) for body, _ in self.transport.deliveries]
            self.assertEqual(bodies[0], bodies[1])
            self.assertEqual(bodies[2]['data']['message_id'], second_mid)
            self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 2})

    async def test_main_capture_precedes_snapshot_and_store_ingest(self):
        from test_main import load_main, Event
        module = load_main(self.temp.name)
        plugin = module.Main(object(), {'enabled': True, 'platform_id': 'instance-1',
            'adapter': 'aiocqhttp', 'self_id': '100', 'sender_id': '200', 'mcp_token': 'x' * 32})
        await plugin.initialize()
        events = events_module.Events(plugin.host.store.db, 'plugin-owner')
        await events.transport.close()
        events.transport = TimingTransport()
        plugin.host.store.events = events
        await events.subscribe(PARAMS)
        original = plugin._snapshot
        def slow_snapshot(event, **kwargs):
            time.sleep(0.01)
            return original(event, **kwargs)
        plugin._snapshot = slow_snapshot
        try:
            await plugin._on_message(Event())
            self.assertTrue(await events.deliver_once())
            sample = events.status()['delivery_latency']['recent'][-1]
            self.assertGreaterEqual(sample['durations_ms']['capture_to_enqueue'], 10)
            self.assertLessEqual(sample['timestamps']['captured']['monotonic_ms'],
                                 sample['timestamps']['ingest_started']['monotonic_ms'])
        finally:
            await events.transport.close()
            await plugin.terminate()


if __name__ == '__main__':
    unittest.main()
