"""Offline durable processing observations; no HTTP, QQ, or external service."""
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
ProcessingWatch = importlib.import_module('astrbot_dot_plugin.processing_watch').ProcessingWatch


def mid(number=1):
    return 'msg_' + format(number, '032x')


def eid(number=1):
    return 'evt_' + format(number, '064x')


class ProcessingWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'inbox.sqlite3')
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''CREATE TABLE messages(id TEXT PRIMARY KEY,completed_at INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE replies(reply_key TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES messages(id),state TEXT);
            CREATE TABLE event_subscriptions(id TEXT PRIMARY KEY,owner TEXT,status TEXT,expires_at REAL);
            CREATE TABLE event_deliveries(event_id TEXT PRIMARY KEY,message_id TEXT,subscription_id TEXT,status TEXT,body BLOB);
            INSERT INTO event_subscriptions VALUES('sid1','owner','active',100000);''')
        self.now = 1000.0
        self.watch = ProcessingWatch(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def capture(self, number=1, *, eligible=True, ack=True, sid='sid1'):
        with self.db:
            self.db.execute('INSERT INTO messages(id) VALUES(?)', (mid(number),))
            self.watch.enroll(mid(number), eligible=eligible)
            self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?,?)',
                (eid(number), mid(number), sid, 'acknowledged' if ack else 'pending', b'original-body'))
            if ack:
                self.watch.note_ack(mid(number), eid(number))
        return mid(number)

    def claim(self, number=1, **kwargs):
        return self.watch.claim(eid(number), enabled=True, owner='owner',
                                authorize=lambda unused: True, **kwargs)

    def test_old_messages_stay_unknown_without_backfill_or_inferred_ack(self):
        with self.db:
            self.db.execute('INSERT INTO messages(id) VALUES(?)', (mid(),))
            self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?,?)',
                            (eid(), mid(), 'sid1', 'acknowledged', b'old'))
        self.watch = ProcessingWatch(self.db, clock=lambda: self.now)
        self.watch.note_started(mid(), 'qq_read_message')
        self.watch.note_read(mid())
        self.watch.note_ack(mid(), eid())
        record = self.watch.get(mid())
        self.assertEqual(record['observation'], 'unknown')
        self.assertIsNone(record['first_ack_at'])
        self.assertIsNone(record['first_read_at'])
        self.assertEqual(self.watch.status()['unknown_messages'], 1)
        self.now += 10000
        self.assertIsNone(self.watch.candidate(enabled=True))

    def test_default_disabled_and_capture_flag_never_promote_old_messages(self):
        self.capture(eligible=False)
        self.now += 500
        self.assertIsNone(self.watch.candidate())
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.assertIsNone(self.watch.claim(eid(), enabled=True, authorize=lambda unused: True))
        self.assertFalse(self.watch.enroll(mid(), eligible=True))
        self.assertFalse(self.watch.get(mid())['eligible_at_capture'])
        self.assertEqual(self.watch.get(mid())['reminder_attempts'], 0)
        self.capture(2)
        self.now += 120
        self.assertIsNone(self.watch.claim(eid(2), authorize=lambda unused: True))
        self.assertEqual(self.watch.get(mid(2))['reminder_attempts'], 0)

    def test_fixed_schedule_two_durable_attempts_and_original_delivery_unchanged(self):
        self.capture()
        original = tuple(self.db.execute('SELECT * FROM event_deliveries').fetchone())
        self.now = 1119.999
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.now = 1120
        self.assertEqual(self.watch.candidate(enabled=True)['event_id'], eid())
        first = self.claim()
        self.assertEqual(first['attempt'], 1)
        self.assertFalse(self.db.in_transaction)
        self.assertTrue(self.watch.preflight(eid(), 1, enabled=True, authorize=lambda unused: True))
        self.watch.finish(eid(), 1, outcome='failed', http_status=503, error='callback_transient')
        self.now = 1419.999
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.now = 1420
        second = self.claim()
        self.assertEqual(second['attempt'], 2)
        self.assertTrue(self.watch.preflight(eid(), 2, enabled=True, authorize=lambda unused: True))
        self.watch.finish(eid(), 2, outcome='acknowledged', http_status=204)
        self.now += 10000
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.assertIsNone(self.claim())
        self.assertEqual(tuple(self.db.execute('SELECT * FROM event_deliveries').fetchone()), original)
        self.assertEqual(self.watch.get(mid())['first_ack_at'], 1000)

    def test_restart_preserves_crash_claim_budget_and_global_pacing(self):
        self.capture()
        self.capture(2)
        self.now = 1120
        self.claim()
        self.db.close()  # Simulate death after commit but before transport/result.
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.watch = ProcessingWatch(self.db, clock=lambda: self.now)
        self.assertEqual(self.watch.get(mid())['reminder_attempts'], 1)
        self.assertEqual(self.watch.get(mid())['reminder_outcome'], 'claimed')
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.now = 1150
        self.assertEqual(self.watch.candidate(enabled=True)['event_id'], eid(2))
        self.claim(2)
        self.now = 1420
        self.assertEqual(self.claim()['attempt'], 2)
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.watch = ProcessingWatch(self.db, clock=lambda: self.now)
        self.now += 10000
        self.assertEqual(self.watch.get(mid())['reminder_attempts'], 2)
        self.assertIsNone(self.claim())

    def test_processing_started_is_not_successful_read_or_completion(self):
        self.capture()
        self.now = 1010
        self.watch.note_started(mid(), 'qq_read_message')
        row = self.watch.get(mid())
        self.assertEqual(row['processing_state'], 'processing_started')
        self.assertEqual(row['processing_started_at'], 1010)
        self.assertIsNone(row['first_read_at'])
        self.assertIsNone(row['stopped_reason'])
        self.now = 1120
        self.assertIsNotNone(self.claim())
        self.now = 1130
        self.watch.note_read(mid())
        row = self.watch.get(mid())
        self.assertEqual(row['processing_state'], 'read')
        self.assertEqual(row['first_read_at'], 1130)
        self.assertEqual(row['unobserved_ack_to_first_read_wall_ms'], 130000)
        self.assertIsNone(row['completed_at'])
        self.assertFalse(self.watch.preflight(eid(), 1, enabled=True, authorize=lambda unused: True))
        self.now = 2000
        self.watch.note_read(mid())
        self.assertEqual(self.watch.get(mid())['first_read_at'], 1130)
        self.assertIsNone(self.claim())

    def test_attachment_start_stops_forever_without_inventing_successful_read(self):
        self.capture()
        self.now += 1
        self.watch.note_started(mid(), 'qq_read_attachment')
        row = self.watch.get(mid())
        self.assertEqual(row['processing_tool'], 'qq_read_attachment')
        self.assertEqual(row['processing_started_at'], 1001)
        self.assertEqual(row['stopped_reason'], 'attachment_processing')
        self.assertIsNone(row['first_read_at'])
        self.now += 10000
        self.assertIsNone(self.claim())

    def test_any_reply_claim_suppresses_even_not_sent_deleted_or_reopened(self):
        for number, state in enumerate(('sending', 'not_sent', 'uncertain', 'adapter_completed'), 1):
            self.capture(number)
            with self.db:
                self.db.execute('INSERT INTO replies VALUES(?,?,?)', ('key'+str(number),mid(number),state))
            self.assertEqual(self.watch.get(mid(number))['stopped_reason'], 'reply_claimed')
            with self.db:
                self.db.execute('DELETE FROM replies WHERE message_id=?', (mid(number),))
                self.db.execute('UPDATE messages SET completed_at=1 WHERE id=?', (mid(number),))
                self.db.execute('UPDATE messages SET completed_at=0 WHERE id=?', (mid(number),))
        self.now += 10000
        self.assertIsNone(self.watch.candidate(enabled=True))

    def test_completion_trigger_stops_and_cascade_removes_only_processing_row(self):
        self.capture()
        with self.db:
            self.db.execute('UPDATE messages SET completed_at=1010 WHERE id=?', (mid(),))
        self.assertEqual(self.watch.get(mid())['completed_at'], 1010)
        self.assertEqual(self.watch.get(mid())['stopped_reason'], 'completed')
        with self.db:
            self.db.execute('DELETE FROM messages WHERE id=?', (mid(),))
        self.assertEqual(self.watch.status()['tracked_messages'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM processing_watch_pacing').fetchone()[0], 1)

    def test_ack_preserved_across_other_subscription_and_new_sub_does_not_inherit(self):
        self.capture()
        self.now = 1010
        with self.db:
            self.db.execute("INSERT INTO event_subscriptions VALUES('sid2','owner','active',100000)")
            self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?,?)',
                            (eid(2),mid(),'sid2','acknowledged',b'other-event'))
            self.watch.note_ack(mid(),eid(2))
            self.db.execute("UPDATE event_subscriptions SET status='cancelled' WHERE id='sid1'")
        row = self.watch.get(mid())
        self.assertEqual(row['first_ack_at'], 1000)
        self.assertEqual(row['event_id'],eid())
        self.now = 2000
        self.assertIsNone(self.watch.candidate(enabled=True))
        self.assertIsNone(self.watch.claim(eid(2),enabled=True,authorize=lambda unused:True))

    def test_no_ack_inference_before_ack_and_read_before_ack_gap_unknown(self):
        self.capture(ack=False)
        self.assertFalse(self.watch.note_ack(mid(),eid()))
        self.watch.note_read(mid())
        self.now = 1100
        with self.db:
            self.db.execute("UPDATE event_deliveries SET status='acknowledged'")
            self.watch.note_ack(mid(),eid())
        row = self.watch.get(mid())
        self.assertEqual(row['first_ack_at'],1100)
        self.assertIsNone(row['unobserved_ack_to_first_read_wall_ms'])
        self.now = 2000
        self.assertIsNone(self.claim())

    def test_live_owner_subscription_expiry_and_authorization_checked_again(self):
        self.capture()
        self.now = 1120
        self.assertIsNone(self.watch.candidate(enabled=True,owner='other-owner'))
        self.assertIsNone(self.watch.claim(eid(),enabled=True,owner='other-owner',authorize=lambda unused:True))
        self.assertEqual(self.watch.get(mid())['reminder_attempts'],0)
        self.claim()
        self.assertFalse(self.watch.preflight(eid(),1,enabled=False,authorize=lambda unused:True))
        self.assertFalse(self.watch.preflight(eid(),1,enabled=True,authorize=lambda unused:False))
        self.assertEqual(self.watch.get(mid())['stopped_reason'],'authorization_revoked')
        self.assertFalse(self.watch.preflight(eid(),1,enabled=True,authorize=lambda unused:True))
        self.capture(2)
        self.now += 120
        self.claim(2)
        with self.db:
            self.db.execute('UPDATE event_subscriptions SET expires_at=?',(self.now,))
        self.assertFalse(self.watch.preflight(eid(2),1,enabled=True,authorize=lambda unused:True))

    def test_authorizer_must_be_sync_literal_true_and_revocation_does_not_block_later(self):
        async def async_validator(unused):
            return True
        def denied(unused):
            raise ValueError('private error')
        validators = [lambda unused:False, lambda unused:None, async_validator, denied, None]
        for number, validator in enumerate(validators, 1):
            self.capture(number)
            self.now += 120
            self.assertIsNone(self.watch.claim(eid(number),enabled=True,authorize=validator))
            row = self.watch.get(mid(number))
            self.assertEqual(row['reminder_attempts'],0)
            self.assertEqual(row['stopped_reason'],'authorization_revoked')
        self.capture(6)
        self.now += 120
        self.assertEqual(self.watch.candidate(enabled=True)['event_id'],eid(6))
        self.assertEqual(self.claim(6)['attempt'],1)
        self.now += 10000
        self.assertIsNone(self.claim(1))

    def test_same_subscription_reactivation_never_revives_old_reminders(self):
        self.capture()
        with self.db:
            self.db.execute("UPDATE event_subscriptions SET status='expired' WHERE id='sid1'")
            self.db.execute("UPDATE event_subscriptions SET status='active',expires_at=100000 WHERE id='sid1'")
        self.now = 1200
        self.assertEqual(self.watch.get(mid())['stopped_reason'],'subscription_inactive')
        self.assertIsNone(self.claim())
        self.capture(2)
        self.now += 120
        self.assertEqual(self.claim(2)['attempt'],1)
        with self.db:
            self.db.execute("DELETE FROM event_subscriptions WHERE id='sid1'")
            self.db.execute("INSERT INTO event_subscriptions VALUES('sid1','owner','active',100000)")
        self.now += 300
        self.assertIsNone(self.claim(2))

    def test_recheck_after_authorizer_mutates_read_or_time(self):
        self.capture()
        self.now = 1120
        def read(unused):
            self.watch.note_read(mid())
            return True
        self.assertIsNone(self.watch.claim(eid(),enabled=True,authorize=read))
        self.assertEqual(self.watch.get(mid())['reminder_attempts'],0)
        self.capture(2)
        self.now = 1240
        self.claim(2)
        def expiry(unused):
            self.now = 100000
            return True
        self.assertFalse(self.watch.preflight(eid(2),1,enabled=True,authorize=expiry))

    def test_oldest_only_global_pacing_and_no_nested_uncommitted_claim(self):
        self.capture()
        self.now += 1
        self.capture(2)
        self.now = 1200
        self.assertIsNone(self.claim(2))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaisesRegex(ValueError,'processing_watch_transaction_busy'):
                self.claim()
        finally:
            self.db.rollback()
        self.assertEqual(self.watch.get(mid())['reminder_attempts'],0)
        self.claim()
        self.assertIsNone(self.claim(2))
        self.now += 29.999
        self.assertIsNone(self.claim(2))
        self.now += 0.001
        self.assertEqual(self.claim(2)['attempt'],1)

    def test_stale_finish_does_not_clobber_newer_attempt_and_sanitizes(self):
        self.capture()
        self.now = 1120
        self.claim()
        self.now = 1420
        self.claim()
        self.assertFalse(self.watch.finish(eid(),1,outcome='acknowledged',http_status=200))
        secret = 'QQ=12345 token=private https://callback.invalid/private'
        self.watch.finish(eid(),2,outcome=secret,http_status=True,error=secret)
        row = self.watch.get(mid())
        self.assertEqual(row['reminder_outcome'],'failed')
        self.assertEqual(row['reminder_error'],'event_operation_failed')
        self.assertIsNone(row['reminder_http_status'])
        self.assertNotIn(secret,json.dumps(self.watch.status()))
        self.assertIsNone(self.watch.get(secret))
        self.assertFalse(self.watch.note_started(mid(),[]))
        self.assertFalse(self.watch.note_ack(mid(),eid(),at=float('nan')))
        self.assertFalse(self.watch.note_ack(mid(),eid(),at=10**1000))

    def test_transaction_rollback_and_failed_claim_write_never_expose_token(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute('INSERT INTO messages(id) VALUES(?)',(mid(),))
        self.watch.enroll(mid(),eligible=True)
        self.db.rollback()
        self.assertEqual(self.watch.status()['tracked_messages'],0)
        self.capture()
        self.now = 1120
        self.db.execute('''CREATE TRIGGER reject_reminder_budget BEFORE UPDATE OF reminder_attempts
            ON message_processing_watch BEGIN SELECT RAISE(ABORT,'test write failed'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.claim()
        self.assertEqual(self.watch.get(mid())['reminder_attempts'],0)
        self.assertIsNone(self.db.execute('SELECT last_claim_at FROM processing_watch_pacing').fetchone()[0])
        self.assertFalse(self.db.in_transaction)

    def test_bounded_status_opaque_ids_only_and_existing_db_limit_unchanged(self):
        self.db.execute('PRAGMA max_page_count=2560')
        before = self.db.execute('PRAGMA max_page_count').fetchone()[0]
        for number in range(1,41):
            self.capture(number,eligible=False)
        self.watch = ProcessingWatch(self.db,clock=lambda:self.now)
        status = self.watch.status(limit=10000)
        self.assertEqual(len(status['recent']),32)
        self.assertEqual(status['tracked_messages'],40)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['redelivery_guaranteed'])
        self.assertNotIn('original-body',json.dumps(status))
        self.assertNotIn('sid1',json.dumps(status))
        self.assertEqual(self.db.execute('PRAGMA max_page_count').fetchone()[0],before)


if __name__ == '__main__':
    unittest.main()
