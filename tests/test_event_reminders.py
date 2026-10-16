"""Offline bounded reminder transport regressions; no QQ or external network I/O."""
import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
events_module = importlib.import_module('astrbot_dot_plugin.events')
host_module = importlib.import_module('astrbot_dot_plugin.host')
Events, EventError = events_module.Events, events_module.EventError
SECRET = 'whsec_' + base64.b64encode(b's' * 32).decode()
URL = 'https://callbacks.example.com/events'
OTHER_URL = 'https://callbacks.example.com/other'


def params(url=URL):
    return {'name': 'qq.message', 'arguments': {}, 'ttlMs': None,
            'delivery': {'mode': 'webhook', 'url': url, 'secret': SECRET}}


class OfflineTransport:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.status = 204
        self.error = None
        self.gate = None
        self.started = asyncio.Event()

    def validate(self, url):
        return events_module.CallbackTransport(['callbacks.example.com']).validate(url)

    async def post(self, url, body, headers, *, preflight=None):
        if self.db.in_transaction:
            raise AssertionError('POST must not run inside a database transaction')
        value = json.loads(body)
        if value.get('type') == 'verification':
            return 200, json.dumps({'challenge': value['challenge']}).encode()
        if preflight is not None and preflight() is not True:
            raise EventError('callback_preflight_rejected')
        self.calls.append((url, body, dict(headers)))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.status, b''

    async def close(self):
        pass


class EventReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'messages.sqlite3'
        self.binding = host_module.Binding('test-platform', 'mock', '100', '200')
        self.store = host_module.Store(self.path, self.binding)
        self.now = 1800000000.0
        self.enabled = True
        self.tasks = []
        await self.attach_events()
        self.sid = (await self.events.subscribe(params()))['id']

    async def attach_events(self):
        self.db = self.store.db
        self.watch = self.store.processing_watch
        self.watch.clock = lambda: self.now
        self.store.reminders_enabled = lambda: self.enabled
        self.events = Events(self.db, 'owner', ['callbacks.example.com'])
        await self.events.transport.close()
        self.events.clock = lambda: self.now
        self.events.processing_watch = self.watch
        self.transport = self.events.transport = OfflineTransport(self.db)
        self.store.events = self.events

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.events.transport.close()
        self.store.close()
        self.temp.cleanup()

    def task(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    def ingest(self, source='one'):
        value = {'source_id': source, 'umo': 'private-route', 'text': 'private message'}
        return self.store.ingest(value)

    def delivery(self, message_id):
        return tuple(self.db.execute('SELECT * FROM event_deliveries WHERE message_id=? ORDER BY rowid',
                                     (message_id,)).fetchone())

    async def ack(self, source='one'):
        message_id, fresh = self.ingest(source)
        self.assertTrue(fresh)
        self.assertTrue(await self.events.deliver_once())
        return message_id

    async def remind(self, validator=None):
        return await self.events.remind_once(validator or (lambda _mid: True),
                                            enabled=lambda: self.enabled)

    def assert_signed(self, call):
        _, body, headers = call
        payload = (headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.').encode() + body
        signature = 'v1,' + base64.b64encode(hmac.new(b's' * 32, payload, hashlib.sha256).digest()).decode()
        self.assertIn(signature, headers['webhook-signature'].split())
        self.assertEqual(headers['webhook-id'], json.loads(body)['eventId'])
        self.assertEqual(set(json.loads(body)['data']), {'message_id'})

    async def test_default_off_and_capture_while_disabled_never_backfills(self):
        self.enabled = False
        message_id = await self.ack()
        original = self.delivery(message_id)
        self.now += 1000
        self.enabled = True
        self.assertFalse(await self.remind())
        self.assertFalse(await self.events.remind_once(lambda _mid: True))
        self.assertFalse(await self.events.remind_once(lambda _mid: True, enabled=True))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.delivery(message_id), original)

    async def test_same_body_id_callback_fresh_signature_and_exact_two_attempt_bound(self):
        message_id = await self.ack()
        original = self.delivery(message_id)
        first = self.transport.calls[0]
        first_ack = self.watch.get(message_id)['first_ack_at']
        self.now += 119.999
        self.assertFalse(await self.remind())
        self.now += 0.001
        self.assertTrue(await self.remind())
        first_claim = self.now
        self.now = first_claim + 299.999
        self.assertFalse(await self.remind())
        self.now = first_claim + 300
        self.assertTrue(await self.remind())
        self.now += 100000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 3)
        for call in self.transport.calls:
            self.assertEqual(call[:2], first[:2])
            self.assertEqual(call[2]['webhook-id'], first[2]['webhook-id'])
            self.assert_signed(call)
        self.assertEqual(len({call[2]['webhook-timestamp'] for call in self.transport.calls}), 3)
        self.assertEqual(self.delivery(message_id), original)
        self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 1})
        self.assertEqual(self.watch.get(message_id)['first_ack_at'], first_ack)
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)

    async def test_failed_reminders_consume_budgets_without_normal_retry_or_subscription_change(self):
        message_id = await self.ack()
        original = self.delivery(message_id)
        self.now += 120
        self.transport.status = 410
        self.assertTrue(await self.remind())
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.events.status()['active_subscriptions'], 1)
        self.now += 300
        self.transport.error = EventError('callback_timeout')
        self.assertTrue(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(self.delivery(message_id), original)
        self.assertEqual(len(self.transport.calls), 3)

    async def test_cancelled_reminder_consumes_attempt_and_only_remaining_slot_survives_restart(self):
        message_id = await self.ack()
        original = self.delivery(message_id)
        self.now += 120
        self.transport.started.clear()
        self.transport.gate = asyncio.Event()
        running = self.task(self.remind())
        await asyncio.wait_for(self.transport.started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.delivery(message_id), original)
        self.store.close()
        self.store = host_module.Store(self.path, self.binding)
        await self.attach_events()
        self.now += 299
        self.assertFalse(await self.remind())
        self.now += 1
        self.assertTrue(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.delivery(message_id), original)

    async def test_normal_delivery_priority_and_persistent_global_minimum_spacing(self):
        first = await self.ack('first')
        self.now += 120
        second, _fresh = self.ingest('second')
        self.assertFalse(await self.remind())
        self.assertTrue(await self.events.deliver_once())
        self.assertTrue(await self.remind())
        self.now += 120
        third = await self.ack('third')
        self.assertTrue(await self.remind())  # second message's first reminder
        self.now += 120
        self.assertTrue(await self.remind())  # third message's first reminder
        self.now += 60  # first's second is now due, >=30 seconds since prior claim.
        self.assertTrue(await self.remind())
        self.now += 120  # second's second is due.
        self.assertTrue(await self.remind())
        self.now += 120  # third's second is due.
        self.assertTrue(await self.remind())
        self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 3})
        self.assertEqual({first, second, third}, {json.loads(call[1])['data']['message_id'] for call in self.transport.calls})

    async def test_global_spacing_blocks_two_due_messages_and_survives_restart(self):
        await self.ack('first')
        await self.ack('second')
        self.now += 120
        self.assertTrue(await self.remind())
        self.assertFalse(await self.remind())
        self.store.close()
        self.store = host_module.Store(self.path, self.binding)
        await self.attach_events()
        self.now += 29.999
        self.assertFalse(await self.remind())
        self.now += 0.001
        self.assertTrue(await self.remind())

    async def test_current_authorization_rechecked_before_post(self):
        await self.ack()
        self.now += 120
        decisions = iter((True, False))
        self.assertTrue(await self.remind(lambda _mid: next(decisions)))
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 300
        self.assertFalse(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)

    async def test_disabling_during_claim_prevents_post_without_refunding_attempt(self):
        await self.ack()
        self.now += 120
        original = self.watch.claim
        def disable_after_claim(*args, **kwargs):
            result = original(*args, **kwargs)
            self.enabled = False
            return result
        self.watch.claim = disable_after_claim
        self.assertTrue(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)
        self.watch.claim = original
        self.enabled = True
        self.now += 300
        self.assertTrue(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())

    async def test_read_attachment_reply_claim_and_completion_each_suppress_reminders(self):
        for number, observe in enumerate((self.watch.note_read, self.watch.note_attachment,
                                           self.watch.note_reply_claim, self.watch.note_complete)):
            with self.subTest(observe=observe.__name__):
                message_id = await self.ack(str(number))
                observe(message_id)
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 4)

    async def test_unsubscribe_expiry_and_callback_identity_change_prevent_post(self):
        message_id = await self.ack()
        self.now += 120
        with self.db:
            self.db.execute('UPDATE event_subscriptions SET url=? WHERE id=?', (OTHER_URL, self.sid))
        self.assertTrue(await self.remind())  # claimed but rejected mismatched callback identity
        self.assertEqual(len(self.transport.calls), 1)
        with self.db:
            self.db.execute('UPDATE event_subscriptions SET url=?,expires_at=? WHERE id=?',
                            (URL, self.now, self.sid))
        self.now += 300
        self.assertFalse(await self.remind())
        self.assertEqual(self.delivery(message_id)[4], 'acknowledged')
        await self.events.subscribe(params())
        await self.events.unsubscribe({'name': 'qq.message', 'arguments': {},
                                      'delivery': {'mode': 'webhook', 'url': URL}})
        self.assertFalse(await self.remind())

    async def test_existing_subscription_lock_and_single_reminder_worker(self):
        await self.ack()
        self.now += 120
        async with self.events._subscription_lock(self.sid):
            self.assertFalse(await self.remind())
        self.transport.started.clear()
        self.transport.gate = asyncio.Event()
        running = self.task(self.remind())
        await asyncio.wait_for(self.transport.started.wait(), 1)
        self.now += 300
        self.assertFalse(await self.remind())
        self.transport.gate.set()
        self.assertTrue(await running)
        self.assertTrue(await self.remind())
        self.assertEqual(self.events._subscription_locks, {})

    async def test_completion_after_post_started_cannot_retract_but_blocks_future_reminder(self):
        message_id = await self.ack()
        original = self.delivery(message_id)
        self.now += 120
        self.transport.started.clear()
        self.transport.gate = asyncio.Event()
        running = self.task(self.remind())
        await asyncio.wait_for(self.transport.started.wait(), 1)
        self.watch.note_complete(message_id)
        self.transport.gate.set()
        self.assertTrue(await running)
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.delivery(message_id), original)

    async def test_late_original_ack_and_duplicate_after_completion_never_revive_processing(self):
        message_id, _fresh = self.ingest()
        self.transport.gate = asyncio.Event()
        running = self.task(self.events.deliver_once())
        await asyncio.wait_for(self.transport.started.wait(), 1)
        with self.db:
            self.db.execute("UPDATE messages SET completed_at=?,completion_reason='manual' WHERE id=?",
                            (int(self.now), message_id))
        self.watch.note_complete(message_id)
        self.now += 16 * 60
        self.transport.gate.set()
        self.assertTrue(await running)
        acknowledged = self.delivery(message_id)
        self.assertEqual(self.ingest(), (message_id, False))
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.delivery(message_id), acknowledged)
        self.assertTrue(self.store.message(message_id)['completed_at'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_original_ack_commits_before_optional_observation(self):
        observed = []
        original = self.watch.note_ack
        def note_ack(*args, **kwargs):
            with sqlite3.connect(self.path) as independent:
                observed.append((self.db.in_transaction,
                    independent.execute('SELECT status FROM event_deliveries').fetchone()[0]))
            return original(*args, **kwargs)
        self.watch.note_ack = note_ack
        await self.ack()
        self.assertEqual(observed, [(False, 'acknowledged')])

    async def test_failed_optional_ack_observation_does_not_requeue_original(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError('private diagnostic error')
        self.watch.note_ack = unavailable
        message_id = await self.ack()
        self.now += 1000
        self.assertFalse(await self.events.deliver_once())
        self.assertFalse(await self.remind())
        self.assertEqual(self.delivery(message_id)[4], 'acknowledged')
        self.assertEqual(len(self.transport.calls), 1)

    async def test_observation_full_rollback_cannot_undo_original_ack_across_restart(self):
        def full(*args, **kwargs):
            self.db.execute('BEGIN IMMEDIATE')
            self.db.rollback()  # SQLITE_FULL may roll back the full writer transaction.
            raise sqlite3.OperationalError('database or disk is full: private path')
        self.watch.note_ack = full
        message_id = await self.ack()
        self.assertEqual(self.events.status()['processing_observation_error'], 'storage_failed')
        self.store.close()
        self.store = host_module.Store(self.path, self.binding)
        await self.attach_events()
        self.now += 1000
        self.assertFalse(await self.events.deliver_once())
        self.assertFalse(await self.remind())
        self.assertEqual(self.delivery(message_id)[4], 'acknowledged')
        self.assertIsNone(self.watch.get(message_id)['first_ack_at'])

    async def test_result_write_failure_preserves_cancel_and_never_refunds_claim(self):
        message_id = await self.ack()
        self.now += 120
        self.transport.started.clear()
        self.transport.gate = asyncio.Event()
        def full(*args, **kwargs):
            raise sqlite3.OperationalError('database or disk is full')
        original = self.watch.finish
        self.watch.finish = full
        running = self.task(self.remind())
        await asyncio.wait_for(self.transport.started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 1)
        self.assertEqual(self.events.status()['processing_observation_error'], 'storage_failed')
        self.watch.finish = original
        self.transport.gate = None
        self.now += 300
        self.assertTrue(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())

    async def test_transport_preflight_rejects_changes_before_request_starts(self):
        message_id = await self.ack()
        self.now += 120
        original = self.transport.post
        async def delayed(url, body, headers, *, preflight=None):
            await asyncio.sleep(0)  # Simulate a DNS/connect yield before request bytes.
            self.watch.note_complete(message_id)
            return await original(url, body, headers, preflight=preflight)
        self.transport.post = delayed
        self.assertTrue(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 1)
        self.assertEqual(self.watch.get(message_id)['reminder_outcome'], 'skipped')
        self.assertEqual(self.watch.get(message_id)['reminder_error'], 'reminder_preflight_failed')

    async def test_multiple_subscriptions_share_message_budget_and_first_ack_identity(self):
        await self.events.subscribe(params(OTHER_URL))
        message_id = await self.ack()
        first = self.transport.calls[0]
        first_ack = self.watch.get(message_id)['first_ack_at']
        self.now += 1
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.watch.get(message_id)['first_ack_at'], first_ack)
        self.now += 120
        self.assertTrue(await self.remind())
        self.now += 300
        self.assertTrue(await self.remind())
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.transport.calls[2][:2], first[:2])
        self.assertEqual(self.transport.calls[3][:2], first[:2])
        self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 2})
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 2)

    async def test_every_persisted_reply_state_suppresses_reminders(self):
        for number, state in enumerate(('sending', 'not_sent', 'uncertain', 'adapter_completed')):
            message_id = await self.ack(str(number))
            key = 'key-' + str(number)
            self.store.claim(message_id, 'outbound draft', key)
            self.store.finish(key, state)
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(len(self.transport.calls), 4)

    async def test_read_after_claim_and_unexpected_transport_error_fail_closed(self):
        message_id = await self.ack()
        self.now += 120
        original = self.watch.claim
        def read_after_claim(*args, **kwargs):
            result = original(*args, **kwargs)
            self.watch.note_read(message_id)
            return result
        self.watch.claim = read_after_claim
        self.assertTrue(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.watch.get(message_id)['reminder_outcome'], 'skipped')
        self.watch.claim = original
        another = await self.ack('second')
        self.now += 120
        self.transport.error = RuntimeError('private transport exception https://private.example/token')
        self.assertTrue(await self.remind())
        state = self.watch.get(another)
        self.assertEqual(state['reminder_attempts'], 1)
        self.assertEqual(state['reminder_outcome'], 'failed')
        self.assertEqual(state['reminder_error'], 'event_operation_failed')
        self.assertNotIn('private transport exception', json.dumps(self.watch.status()))

    async def test_invalid_authorization_results_do_not_consume_reminder_budget(self):
        for number, result in enumerate((False, None, 1, 'yes')):
            message_id = await self.ack(str(number))
            self.now += 120
            self.assertFalse(await self.remind(lambda _mid: result))
            self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 0)
            self.assertEqual(self.watch.get(message_id)['stopped_reason'], 'authorization_revoked')
        async def async_validator(_mid):
            return True
        message_id = await self.ack('async')
        self.now += 120
        self.assertFalse(await self.remind(async_validator))
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 0)
        self.assertEqual(len(self.transport.calls), 5)

    async def test_same_callback_resubscription_does_not_revive_old_ack(self):
        message_id = await self.ack()
        original = self.delivery(message_id)
        await self.events.unsubscribe({'name': 'qq.message', 'arguments': {},
                                      'delivery': {'mode': 'webhook', 'url': URL}})
        self.assertEqual((await self.events.subscribe(params()))['id'], self.sid)
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(self.delivery(message_id), original)
        self.assertEqual(self.watch.get(message_id)['stopped_reason'], 'subscription_inactive')
        self.assertEqual(len(self.transport.calls), 1)

    async def test_new_normal_delivery_during_connect_takes_priority_before_reminder_post(self):
        message_id = await self.ack()
        self.now += 120
        original = self.transport.post
        async def delayed(url, body, headers, *, preflight=None):
            await asyncio.sleep(0)
            self.ingest('new-priority-message')
            return await original(url, body, headers, preflight=preflight)
        self.transport.post = delayed
        self.assertTrue(await self.remind())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.watch.get(message_id)['reminder_attempts'], 1)
        self.assertEqual(self.watch.get(message_id)['reminder_outcome'], 'skipped')
        self.transport.post = original
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(len(self.transport.calls), 2)

    async def test_events_without_watch_keep_existing_delivery_behavior(self):
        self.events.processing_watch = None
        await self.ack()
        self.now += 1000
        self.assertFalse(await self.remind())
        self.assertEqual(self.events.status()['deliveries'], {'acknowledged': 1})


if __name__ == '__main__':
    unittest.main()
