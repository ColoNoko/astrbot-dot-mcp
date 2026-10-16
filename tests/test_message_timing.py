"""Offline lifecycle tests: no QQ adapter, NAS, or external callbacks."""
import asyncio
import base64
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
timing_module = importlib.import_module('astrbot_dot_plugin.message_timing')
host_module = importlib.import_module('astrbot_dot_plugin.host')
events_module = importlib.import_module('astrbot_dot_plugin.events')
MessageTiming = timing_module.MessageTiming


def mid(number=1):
    return 'msg_' + format(number, '032x')


def eid(number=1):
    return 'evt_' + format(number, '064x')


def rid(number=1):
    return 'read_' + format(number, '032x')


def stamp(utc, mono):
    return {'utc_ms': utc, 'monotonic_ms': mono}


class MessageTimingTests(unittest.TestCase):
    def setUp(self):
        self.timing = MessageTiming()
        self.now = (1000, 1000)
        self.timing.stamp = lambda: self.now

    def sample(self):
        return self.timing.status()['recent'][-1]

    def event(self, number=1, **extra):
        sample = {'message_id': mid(), 'event_id': eid(number), 'attempt': 1,
                  'outcome': 'acknowledged', 'http_status': 204,
                  'timestamps': {'transport_started': stamp(930, 930),
                                 'transport_finished': stamp(950, 950),
                                 'callback_ack': stamp(950, 950)}}
        sample.update(extra)
        return sample

    def test_correlated_capture_ack_read_monotonic_wall_jump_and_snapshot_copy(self):
        self.timing.note_ingest(mid(), captured_at=(1100, 800), ingest_at=(900, 850), enqueued_at=(920, 920))
        self.timing.note_event(self.event())
        self.now = (700, 1100)  # UTC moves backward, monotonic still orders observations.
        self.timing.note_read(mid())
        sample = self.sample()
        self.assertEqual(sample['message_id'], mid())
        self.assertEqual(sample['durations_ms']['unobserved_ack_to_first_read_ms'], 150)
        self.assertEqual(sample['durations_ms']['capture_to_first_read_ms'], 300)
        self.assertEqual(sample['timestamps']['captured']['utc_ms'], 1100)
        sample['timestamps']['captured']['utc_ms'] = 0
        sample['event_attempts'][0]['ack']['utc_ms'] = 0
        self.assertEqual(self.sample()['timestamps']['captured']['utc_ms'], 1100)
        self.assertEqual(self.sample()['event_attempts'][0]['ack']['utc_ms'], 950)
        self.now = (1200, 1200)
        self.timing.note_read(mid())
        self.assertEqual(self.sample()['timestamps']['first_tool_read']['monotonic_ms'], 1100)

    def test_unknown_restart_and_read_before_ack_do_not_invent_durations(self):
        self.timing.note_read(mid())
        self.assertIsNone(self.sample()['timestamps']['captured'])
        self.assertIsNone(self.sample()['durations_ms']['unobserved_ack_to_first_read_ms'])
        self.now = (2000, 2000)
        event = self.event(timestamps={'transport_started': stamp(1300, 1300),
            'transport_finished': stamp(1500, 1500), 'callback_ack': stamp(1500, 1500)})
        self.timing.note_event(event)
        self.assertIsNone(self.sample()['durations_ms']['unobserved_ack_to_first_read_ms'])
        fresh = MessageTiming()
        fresh.note_read(mid())
        self.assertIsNone(fresh.status()['recent'][0]['timestamps']['events_ack'])

    def test_ids_enums_numbers_and_stage_whitelists_redact_input(self):
        private = 'QQ=12345 token=secret https://private.example/path reply_key=secret message=private'
        for invalid in ('msg_secret', 'msg_' + 'a' * 32 + '\n', 'MSG_' + 'a' * 32, private, None, [], True):
            self.timing.note_read(invalid)
        self.assertEqual(self.timing.status()['retained'], 0)
        self.timing.note_ingest(mid(), captured_at=(private, 1), ingest_at=(1, True))
        event = self.event(outcome=private, http_status=private)
        event['private'] = private
        self.timing.note_event(event)
        self.timing.note_attachment(mid(), {'read_id': rid(), 'outcome': private, 'stage': private,
            'elapsed_ms': 2**70, 'url': private, 'stages': [
                {'stage': 'queued', 'elapsed_ms': 5, 'private': private},
                {'stage': 'adapter_api', 'elapsed_ms': 2, 'api_action': private, 'outcome': private},
                {'stage': private, 'elapsed_ms': 3}]})
        sample = self.sample()
        self.assertIsNone(sample['timestamps']['captured'])
        self.assertEqual(sample['event_attempts'][0]['outcome'], 'unknown')
        self.assertIsNone(sample['event_attempts'][0]['http_status'])
        self.assertEqual(sample['attachment_reads'][0]['queue_wait_ms'], 5)
        self.assertEqual(len(sample['attachment_reads'][0]['stages']), 2)
        self.assertNotIn(private, json.dumps(self.timing.status()))

    def test_all_rings_bounded_and_finished_operations_immutable(self):
        for number in range(1, 41):
            self.timing.note_read(mid(number))
        status = self.timing.status()
        self.assertEqual(status['limit'], 32)
        self.assertEqual(status['retained'], 32)
        self.assertEqual(status['recent'][0]['message_id'], mid(9))
        for number in range(1, 15):
            self.timing.note_event(self.event(number, message_id=mid(40)))
            snapshot = {'read_id': rid(number), 'outcome': 'running', 'stage': 'queued', 'elapsed_ms': 0}
            self.timing.note_attachment(mid(40), snapshot)
            self.timing.note_attachment(mid(40), {**snapshot, 'outcome': 'succeeded', 'elapsed_ms': 10})
            token = self.timing.begin_reply(mid(40))
            self.timing.finish_reply(mid(40), token, 'not_sent')
        sample = self.sample()
        for key in ('event_attempts', 'attachment_reads', 'reply_attempts'):
            self.assertEqual(len(sample[key]), 8)
        before = self.timing.status()
        self.timing.note_reply_stage(mid(40), token, 'adapter_started')
        self.timing.finish_reply(mid(40), token, 'adapter_completed')
        self.timing.note_attachment(mid(40), {**snapshot, 'outcome': 'failed', 'elapsed_ms': 999})
        self.timing.note_event(self.event(14, message_id=mid(40), outcome='failed'))
        self.assertEqual(self.timing.status(), before)
        self.assertNotIn('_token', json.dumps(before))

    def test_invalid_attempt_cannot_overwrite_valid_running_event(self):
        self.timing.note_event_started(mid(), eid(), 1, {'transport_started': (900, 900)})
        self.timing.note_event(self.event(attempt=True, outcome='failed'))
        self.assertEqual(self.sample()['event_attempts'][0]['outcome'], 'sending')


class StoreTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'messages.db'
        self.binding = host_module.Binding('private-platform', 'mock', '100', '200')
        self.store = host_module.Store(self.path, self.binding)
        self.value = {'source_id': 'private-source', 'umo': 'private-route', 'text': 'private-payload'}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_duplicate_failed_ingest_restart_and_schema_unchanged(self):
        schema = self.store.db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        ident, fresh = self.store.ingest(self.value, captured_at=self.store.message_timing.stamp())
        self.assertTrue(fresh)
        before = self.store.message_timing.status()
        self.assertEqual(self.store.ingest(self.value), (ident, False))
        self.assertEqual(self.store.message_timing.status(), before)
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('private exception')
        self.store.events = types.SimpleNamespace(enqueue=fail)
        with self.assertRaises(host_module.Rejected):
            self.store.ingest({**self.value, 'source_id': 'second'})
        self.assertEqual(self.store.message_timing.status(), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall(), schema)
        self.store.close()
        self.store = host_module.Store(self.path, self.binding)
        self.assertEqual(self.store.message_timing.status()['retained'], 0)
        self.store.read(ident)  # A Store read for internal work is not a tool observation.
        self.assertEqual(self.store.message_timing.status()['retained'], 0)
        self.store.message_timing.note_read(ident)
        sample = self.store.message_timing.status()['recent'][0]
        self.assertIsNone(sample['timestamps']['captured'])
        self.assertIsNone(sample['durations_ms']['capture_to_first_read_ms'])


class HostTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = host_module.Store(Path(self.temp.name) / 'db', host_module.Binding('instance', 'mock', '100', '200'))
        self.mid, _ = self.store.ingest({'source_id': 'private-source', 'umo': 'private-route', 'text': 'private-payload'},
                                      captured_at=self.store.message_timing.stamp())
        self.sent = []
        async def preflight(row):
            await asyncio.sleep(0.002)
            return 'target'
        async def send(target, row, text):
            self.sent.append(text)
            await asyncio.sleep(0.002)
            return True
        self.host = host_module.Host(self.store, preflight, send, timeout=0.025)

    async def asyncTearDown(self):
        await self.host.close()
        self.store.close()
        self.temp.cleanup()

    def sample(self):
        return self.store.message_timing.status()['recent'][0]['reply_attempts'][-1]

    async def test_success_duplicate_reply_and_raw_exception_redaction(self):
        self.store.message_timing.note_read(self.mid)
        result = await self.host.reply(self.mid, 'private-outbound', 'private-key')
        self.assertEqual(result['state'], 'adapter_completed')
        sample = self.sample()
        self.assertGreaterEqual(sample['durations_ms']['preflight'], 1)
        self.assertGreaterEqual(sample['durations_ms']['adapter'], 1)
        self.assertIsNotNone(sample['adapter_finished'])
        await self.host.reply(self.mid, 'private-outbound', 'private-key')
        self.assertEqual(self.sample(), sample)
        self.assertEqual(self.sent, ['private-outbound'])
        async def fail(*args):
            raise RuntimeError('token=secret raw-private-exception')
        self.host.send = fail
        self.assertEqual((await self.host.reply(self.mid, 'private-outbound', 'other-key'))['state'], 'uncertain')
        rendered = json.dumps(self.store.message_timing.status())
        for private in ('private-key', 'other-key', 'private-outbound', 'private-payload', 'token=secret', 'raw-private-exception'):
            self.assertNotIn(private, rendered)

    async def test_failed_preflight_is_not_sent_without_adapter_time(self):
        async def fail(row):
            raise RuntimeError('private details')
        self.host.preflight = fail
        self.assertEqual((await self.host.reply(self.mid, 'x', 'key'))['state'], 'not_sent')
        sample = self.sample()
        self.assertEqual(sample['state'], 'not_sent')
        self.assertIsNotNone(sample['preflight_finished'])
        self.assertIsNone(sample['adapter_started'])
        self.assertEqual(self.sent, [])

    async def test_preflight_timeout_and_late_suppressed_cancellation_cannot_send_or_mutate(self):
        release = asyncio.Event()
        async def late(row):
            try:
                await asyncio.wait_for(release.wait(), 2)
            except asyncio.CancelledError:
                await asyncio.wait_for(release.wait(), 2)
            return 'target'
        self.host.preflight = late
        self.assertEqual((await self.host.reply(self.mid, 'x', 'key'))['state'], 'not_sent')
        before = self.sample()
        self.assertIsNone(before['preflight_finished'])
        release.set()
        await asyncio.sleep(0.01)
        self.assertEqual(self.sample(), before)
        self.assertEqual(self.sent, [])
        await self.host.reply(self.mid, 'x', 'key')
        self.assertEqual(self.sample(), before)

    async def test_send_timeout_and_late_completion_remain_uncertain(self):
        release = asyncio.Event()
        async def late(*args):
            self.sent.append('sent')
            try:
                await asyncio.wait_for(release.wait(), 2)
            except asyncio.CancelledError:
                await asyncio.wait_for(release.wait(), 2)
            return True
        self.host.send = late
        self.assertEqual((await self.host.reply(self.mid, 'x', 'key'))['state'], 'uncertain')
        before = self.sample()
        self.assertIsNotNone(before['adapter_started'])
        self.assertIsNone(before['adapter_finished'])
        release.set()
        await asyncio.sleep(0.01)
        self.assertEqual(self.sample(), before)
        await self.host.reply(self.mid, 'x', 'key')
        self.assertEqual(self.sent, ['sent'])

    async def test_caller_cancellation_freezes_not_sent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def late(row):
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), 2)
            except asyncio.CancelledError:
                await asyncio.wait_for(release.wait(), 2)
            return 'target'
        self.host.preflight = late
        task = asyncio.create_task(self.host.reply(self.mid, 'x', 'key'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        before = self.sample()
        self.assertEqual(before['state'], 'not_sent')
        release.set()
        await asyncio.sleep(0.01)
        self.assertEqual(self.sample(), before)
        self.assertEqual(self.sent, [])


class EventTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_start_and_ack_link_to_committed_message_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            store = host_module.Store(Path(directory) / 'db', host_module.Binding('instance', 'mock', '100', '200'))
            events = events_module.Events(store.db, 'private-owner', message_timing=store.message_timing)
            await events.transport.close()
            entered, release = asyncio.Event(), asyncio.Event()
            class Transport:
                last_transport = None
                async def post(self, *args):
                    entered.set()
                    await asyncio.wait_for(release.wait(), 2)
                    return 204, b''
            events.transport = Transport()
            store.events = events
            with store.db:
                # Synthetic repeated bytes for offline Standard Webhooks signing only.
                # Construct at runtime; this is not a provider-issued credential.
                test_secret = "whsec_" + base64.b64encode(bytes([115]) * 32).decode("ascii")
                store.db.execute("INSERT INTO event_subscriptions VALUES ('sub-private','private-owner','https://private.example',?,NULL,NULL,NULL,9999999999,'active')", (test_secret,))
            ident, _ = store.ingest({'source_id': 'private-source', 'umo': 'private-route', 'text': 'private-payload'},
                                    captured_at=store.message_timing.stamp())
            task = asyncio.create_task(events.deliver_once())
            await entered.wait()
            sample = store.message_timing.status()['recent'][0]
            self.assertEqual(sample['message_id'], ident)
            self.assertEqual(sample['event_attempts'][0]['outcome'], 'sending')
            self.assertIsNotNone(sample['timestamps']['events_transport_started'])
            self.assertIsNone(sample['timestamps']['events_ack'])
            release.set()
            self.assertTrue(await task)
            store.message_timing.note_read(ident)
            sample = store.message_timing.status()['recent'][0]
            self.assertEqual(sample['event_attempts'][0]['outcome'], 'acknowledged')
            self.assertIsNotNone(sample['timestamps']['events_ack'])
            self.assertIsNotNone(sample['durations_ms']['unobserved_ack_to_first_read_ms'])
            for private in ('private-owner', 'private.example', 'private-source', 'private-route', 'private-payload', 'sub-private'):
                self.assertNotIn(private, json.dumps(store.message_timing.status()))
            store.close()


class AttachmentTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reads_keep_local_ids_and_queue_wait_after_last_read_changes(self):
        module = importlib.import_module('astrbot_dot_plugin.attachments')
        reader = module.Attachments.__new__(module.Attachments)
        reader.transport = types.SimpleNamespace(attachment_dns='system')
        reader.message_timing = MessageTiming()
        reader._lock = asyncio.Lock()
        reader._readers = set()
        first_entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def offline_read(*args, **kwargs):
            nonlocal calls
            async with reader._lock:
                module._attachment_stage('validation')
                calls += 1
                if calls == 1:
                    first_entered.set()
                    await asyncio.wait_for(release.wait(), 2)
                return {'content': [], 'isError': False}
        reader._read = offline_read
        first = asyncio.create_task(reader.read(mid(), 'att_' + '1' * 32))
        await asyncio.wait_for(first_entered.wait(), 2)
        first_id = reader._last_read.read_id
        second = asyncio.create_task(reader.read(mid(), 'att_' + '2' * 32))
        await asyncio.sleep(0.02)
        second_id = reader._last_read.read_id
        self.assertNotEqual(first_id, second_id)
        release.set()
        await asyncio.gather(first, second)
        samples = reader.message_timing.status()['recent'][0]['attachment_reads']
        self.assertEqual([sample['read_id'] for sample in samples], [first_id, second_id])
        self.assertEqual([sample['outcome'] for sample in samples], ['succeeded', 'succeeded'])
        self.assertGreaterEqual(samples[1]['queue_wait_ms'], 10)
        self.assertEqual(reader._last_read.read_id, second_id)
        self.assertTrue(all(sample['started'] is not None and sample['finished'] is not None for sample in samples))


if __name__ == '__main__':
    unittest.main()
