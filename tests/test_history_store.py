"""Synthetic-only bounded history, migration, isolation and disk-limit tests."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from history_store import BoundedHistory, configure_storage, HISTORY_LIMIT_BYTES, storage_error


class HistoryError(ValueError):
    pass


def schema(db):
    db.executescript('''CREATE TABLE messages(
        id TEXT PRIMARY KEY,source_id TEXT NOT NULL,umo TEXT NOT NULL,text TEXT NOT NULL,
        created INTEGER NOT NULL,conversation_type TEXT NOT NULL,group_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,media_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE(conversation_type,group_id,sender_id,source_id));
        CREATE TABLE replies(reply_key TEXT PRIMARY KEY,
        message_id TEXT NOT NULL REFERENCES messages(id),text TEXT NOT NULL,state TEXT NOT NULL);''')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=.05)
        self.db.row_factory = sqlite3.Row
        configure_storage(self.db, self.path, error_cls=HistoryError)
        schema(self.db)
        self.history = BoundedHistory(self.db, self.path, error_cls=HistoryError)
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def value(self, **extra):
        self.n += 1
        result = {'source_id': str(self.n), 'umo': 'synthetic-private', 'text': 'hello',
            'conversation_type': 'private', 'group_id': '', 'sender_id': '200', 'attachments': []}
        result.update(extra)
        return result

    def insert(self, value=None, *, created=None, completed=False, admit=True):
        value = value or self.value()
        if admit:
            self.history.admit(value)
        mid = 'msg_' + value['source_id'].zfill(12)
        with self.db:
            self.db.execute('''INSERT INTO messages(id,source_id,umo,text,created,
                conversation_type,group_id,sender_id,media_json) VALUES(?,?,?,?,?,?,?,?,?)''',
                (mid, value['source_id'], value['umo'], value['text'], int(time.time()) if created is None else created,
                 value['conversation_type'], value['group_id'], value['sender_id'],
                 json.dumps(value.get('attachments', []), ensure_ascii=False, sort_keys=True, separators=(',', ':'))))
        if completed:
            self.history.complete(mid)
        return mid

    def reply(self, mid, key, state='adapter_completed', text='reply'):
        self.history.admit_reply(mid, text, key)
        with self.db:
            self.db.execute('INSERT INTO replies VALUES(?,?,?,?)', (key, mid, text, state))
            self.history.finish_reply(key, state)

    def event_table(self):
        with self.db:
            self.db.execute('CREATE TABLE event_deliveries(event_id TEXT PRIMARY KEY,message_id TEXT,status TEXT,body BLOB)')

    def test_default_cap_is_exact_mib_and_wal_disabled(self):
        status = self.history.status()
        self.assertEqual(status['limit_bytes'], 10_485_760)
        self.assertEqual(status['journal_mode'], 'delete')
        self.assertTrue(status['temporary_journal_excluded'])
        self.assertEqual(self.db.execute('PRAGMA max_page_count').fetchone()[0] * self.db.execute('PRAGMA page_size').fetchone()[0], HISTORY_LIMIT_BYTES)
        self.assertLessEqual(status['used_bytes'], HISTORY_LIMIT_BYTES)
        self.assertEqual(status['wal_bytes'], 0)

    def test_read_and_webhook_ack_do_not_complete(self):
        mid = self.insert()
        self.event_table()
        with self.db:
            self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?)', ('event', mid, 'acknowledged', b'opaque'))
        self.assertFalse(self.history.page('private')['messages'][0]['completed'])
        self.assertEqual(self.history.prune(), 0)
        self.assertEqual(self.history.status()['pending_messages'], 1)

    def test_successful_reply_completes_uncertain_and_not_sent_do_not(self):
        for state in ('uncertain', 'not_sent', 'adapter_completed'):
            mid = self.insert()
            self.reply(mid, 'key-' + state, state)
            row = self.db.execute('SELECT completed_at FROM messages WHERE id=?', (mid,)).fetchone()
            self.assertEqual(bool(row[0]), state == 'adapter_completed')
        self.assertEqual(self.history.status()['pending_messages'], 2)
        self.assertEqual(self.history.prune(), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_explicit_completion_releases_uncertain_but_not_in_flight(self):
        first, second = self.insert(), self.insert()
        self.reply(first, 'uncertain', 'uncertain')
        self.reply(second, 'sending', 'sending')
        self.history.complete(first)
        self.history.complete(second)
        self.assertEqual(self.history.prune(), 1)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM messages WHERE id=?', (second,)).fetchone())
        self.assertEqual(self.history.lookup_reply(first, 'reply', 'uncertain'), ('uncertain', False))

    def test_pending_events_pin_completed_messages(self):
        self.event_table()
        mids = []
        for state in ('pending', 'retry', 'sending', 'failed', 'cancelled', 'acknowledged'):
            mid = self.insert(completed=True)
            mids.append(mid)
            with self.db:
                self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?)', ('e' + state, mid, state, b'opaque'))
        self.assertEqual(self.history.prune(), 3)
        remaining = [row[0] for row in self.db.execute('SELECT id FROM messages ORDER BY id')]
        self.assertEqual(remaining, mids[:3])
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_deliveries').fetchone()[0], 3)

    def test_oldest_completed_pruned_first_pending_preserved(self):
        pending = self.insert(created=1)
        newest = self.insert(created=40, completed=True)
        oldest = self.insert(created=20, completed=True)
        self.insert(created=30, completed=True)
        self.assertEqual(self.history.prune(max_messages=1), 1)
        self.assertIsNone(self.db.execute('SELECT 1 FROM messages WHERE id=?', (oldest,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM messages WHERE id=?', (pending,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM messages WHERE id=?', (newest,)).fetchone())

    def test_source_and_reply_tombstones_preserve_finite_dedupe(self):
        value = self.value()
        mid = self.insert(value)
        self.reply(mid, 'reply-key')
        self.assertEqual(self.history.lookup_duplicate(value), (mid, False))
        self.history.prune()
        self.assertEqual(self.history.lookup_duplicate(value), (mid, False))
        self.assertEqual(self.history.lookup_reply(mid, 'reply', 'reply-key'), ('adapter_completed', False))
        with self.assertRaisesRegex(HistoryError, 'source_id_collision'):
            self.history.lookup_duplicate(dict(value, text='changed'))
        with self.assertRaisesRegex(HistoryError, 'reply_key_conflict'):
            self.history.lookup_reply(mid, 'changed', 'reply-key')
        with self.assertRaisesRegex(HistoryError, 'reply_key_conflict'):
            self.history.lookup_reply('other-message', 'reply', 'reply-key')
        now = int(time.time())
        self.history.clock = lambda: now + self.history.tombstone_ttl + 10
        self.assertIsNone(self.history.lookup_duplicate(value))
        self.assertIsNone(self.history.lookup_reply(mid, 'reply', 'reply-key'))

    def test_media_fingerprint_includes_captured_source_and_persists_tombstone(self):
        value = self.value(attachments=[{'kind': 'image', 'url': 'https://synthetic.invalid/a', 'segment_index': 0}])
        mid = self.insert(value, completed=True)
        self.assertEqual(self.history.lookup_duplicate(value), (mid, False))
        changed = dict(value, attachments=[{'kind': 'image', 'url': 'https://synthetic.invalid/b', 'segment_index': 0}])
        with self.assertRaisesRegex(HistoryError, 'source_id_collision'):
            self.history.lookup_duplicate(changed)
        self.history.prune()
        self.assertEqual(self.history.lookup_duplicate(value), (mid, False))
        with self.assertRaisesRegex(HistoryError, 'source_id_collision'):
            self.history.lookup_duplicate(changed)
        tombstone_bytes = repr(self.db.execute('SELECT * FROM history_tombstones').fetchall())
        self.assertNotIn('synthetic.invalid', tombstone_bytes)

    def test_tombstone_joint_count_bound(self):
        self.history.max_tombstones = 5
        for _ in range(10):
            mid = self.insert()
            self.reply(mid, 'key-' + mid)
        self.history.prune()
        self.assertLessEqual(self.history.status()['tombstones'], 5)

    def test_scope_pagination_stable_with_ties_and_cross_scope_rejected(self):
        mids = [self.insert(created=100) for _ in range(5)]
        group_one = self.insert(self.value(conversation_type='group', group_id='123', sender_id='201'), created=100)
        group_two = self.insert(self.value(conversation_type='group', group_id='456', sender_id='201'), created=100)
        first = self.history.page('private', limit=2)
        second = self.history.page('private', limit=2, cursor=first['next_cursor'])
        third = self.history.page('private', limit=2, cursor=second['next_cursor'])
        returned = [row['message_id'] for page in (first, second, third) for row in page['messages']]
        self.assertEqual(returned, list(reversed(mids)))
        self.assertIsNone(third['next_cursor'])
        self.assertEqual([row['message_id'] for row in self.history.page('group', '123')['messages']], [group_one])
        self.assertEqual([row['message_id'] for row in self.history.page('group', '456')['messages']], [group_two])
        with self.assertRaisesRegex(HistoryError, 'invalid_history_cursor'):
            self.history.page('group', '123', cursor=first['next_cursor'])
        for args in (('private', '123'), ('group', ''), ('group', "1' OR 1=1"), ('x', '')):
            with self.assertRaisesRegex(HistoryError, 'invalid_history_scope'):
                self.history.page(*args)
        for bad in ('not/base64!!!', 1, 'a' * 1025):
            with self.assertRaisesRegex(HistoryError, 'invalid_history_cursor'):
                self.history.page('private', cursor=bad)
        with self.assertRaisesRegex(HistoryError, 'invalid_history_limit'):
            self.history.page('private', limit=True)

    def test_reply_output_bounded_without_removing_persisted_replies(self):
        mid = self.insert()
        with self.db:
            for number in range(25):
                self.db.execute('INSERT INTO replies VALUES(?,?,?,?)',
                                ('key' + str(number), mid, 'reply' + str(number), 'not_sent'))
        message = self.history.page('private')['messages'][0]
        self.assertEqual(len(message['replies']), 20)
        self.assertTrue(message['replies_truncated'])
        self.assertEqual(message['replies'][0]['text'], 'reply5')
        self.assertEqual(message['replies'][-1]['text'], 'reply24')
        self.assertEqual(self.db.execute('SELECT count(*) FROM replies').fetchone()[0], 25)

    def test_conversations_group_by_group_not_sender_and_paginate(self):
        self.insert()
        self.insert(self.value(conversation_type='group', group_id='123', sender_id='201'))
        self.insert(self.value(conversation_type='group', group_id='123', sender_id='202'))
        self.insert(self.value(conversation_type='group', group_id='456', sender_id='201'))
        result = self.history.conversations(limit=1)
        gathered = result['conversations']
        while result['next_cursor']:
            result = self.history.conversations(limit=1, cursor=result['next_cursor'])
            gathered.extend(result['conversations'])
        self.assertEqual(len(gathered), 3)
        self.assertEqual(gathered[0]['group_id'], '123')
        self.assertEqual(gathered[0]['message_count'], 2)
        self.assertEqual(gathered[-1]['conversation_type'], 'private')

    def test_small_cap_rejects_new_pending_never_silently_evicts(self):
        small = 512 * 1024
        configure_storage(self.db, self.path, max_bytes=small, error_cls=HistoryError)
        self.history = BoundedHistory(self.db, self.path, max_bytes=small, error_cls=HistoryError)
        accepted = []
        for _ in range(100):
            try:
                accepted.append(self.insert(self.value(text='x' * 16384)))
            except HistoryError as error:
                self.assertEqual(str(error), 'storage_full')
                break
        else:
            self.fail('expected bounded admission rejection')
        self.assertGreater(len(accepted), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], len(accepted))
        # Each admitted message can still receive a maximum-size reply.
        for mid in accepted:
            self.reply(mid, 'key-' + mid, 'uncertain', 'r' * 16384)
        self.assertEqual(self.db.execute('SELECT count(*) FROM replies').fetchone()[0], len(accepted))
        self.assertLessEqual(self.path.stat().st_size, small)
        self.assertEqual(self.history.status()['pending_messages'], len(accepted))

    def test_capacity_pressure_prunes_only_completed_and_keeps_rolling(self):
        cap = 512 * 1024
        configure_storage(self.db, self.path, max_bytes=cap, error_cls=HistoryError)
        self.history = BoundedHistory(self.db, self.path, max_bytes=cap, error_cls=HistoryError, max_tombstones=32)
        pending = self.insert(self.value(text='pending'))
        peak = 0
        for _ in range(60):
            self.insert(self.value(text='x' * 16384), completed=True)
            peak = max(peak, self.path.stat().st_size)
        self.assertGreater(self.history.pruned_count, 0)
        self.assertLessEqual(peak, cap)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM messages WHERE id=?', (pending,)).fetchone())
        self.assertEqual(self.history.status()['pending_messages'], 1)

    def test_shared_event_table_writes_obey_hard_page_limit(self):
        cap = 256 * 1024
        configure_storage(self.db, self.path, max_bytes=cap, error_cls=HistoryError)
        self.event_table()
        with self.assertRaises(sqlite3.DatabaseError) as caught:
            with self.db:
                for i in range(1000):
                    self.db.execute('INSERT INTO event_deliveries VALUES(?,?,?,?)', (str(i), 'opaque', 'pending', b'x' * 16384))
        self.assertEqual(storage_error(caught.exception), 'storage_full')
        self.assertEqual(self.db.execute('SELECT count(*) FROM event_deliveries').fetchone()[0], 0)
        self.assertLessEqual(self.path.stat().st_size, cap)
        self.assertEqual(Path(str(self.path) + '-wal').exists(), False)

    def test_attachment_children_deleted_atomically(self):
        with self.db:
            self.db.execute('''CREATE TABLE message_attachments(id TEXT PRIMARY KEY,
                message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,source_url TEXT)''')
        mid = self.insert(completed=True)
        with self.db:
            self.db.execute('INSERT INTO message_attachments VALUES(?,?,?)', ('opaque', mid, 'https://synthetic.invalid/a'))
        self.assertEqual(self.history.prune(), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM message_attachments').fetchone()[0], 0)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_reopen_preserves_completed_columns_and_finite_tombstones(self):
        value = self.value()
        mid = self.insert(value, completed=True)
        self.history.prune()
        pending = self.insert()
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        configure_storage(self.db, self.path, error_cls=HistoryError)
        self.history = BoundedHistory(self.db, self.path, error_cls=HistoryError)
        self.assertEqual(self.history.lookup_duplicate(value), (mid, False))
        self.assertEqual(self.history.page('private')['messages'][0]['message_id'], pending)
        self.assertFalse(self.history.page('private')['messages'][0]['completed'])


class MigrationTests(unittest.TestCase):
    def test_upgrade_old_schema_preserves_ids_and_only_infers_successful_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            schema(db)
            with db:
                for index, state in enumerate(('adapter_completed', 'uncertain', 'not_sent')):
                    mid = 'msg_' + str(index)
                    db.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)''', (mid, str(index), 'umo', 'text', 100 + index, 'private', '', '200', '[]'))
                    db.execute('INSERT INTO replies VALUES(?,?,?,?)', ('key' + str(index), mid, 'reply', state))
            configure_storage(db, path)
            history = BoundedHistory(db, path)
            self.assertEqual(history.status()['completed_messages'], 1)
            self.assertEqual(history.status()['pending_messages'], 2)
            self.assertEqual([row[0] for row in db.execute('SELECT id FROM messages ORDER BY id')], ['msg_0', 'msg_1', 'msg_2'])
            history = BoundedHistory(db, path)
            self.assertEqual(history.status()['completed_messages'], 1)
            db.close()

    def test_oversized_legacy_refused_without_changing_file_or_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE retained(value BLOB)')
            with db:
                db.execute('INSERT INTO retained VALUES(?)', (b'x' * (200 * 1024),))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'history_migration_requires_space'):
                configure_storage(db, path, max_bytes=128 * 1024)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(db.execute('SELECT length(value) FROM retained').fetchone()[0], 200 * 1024)
            db.close()

    def test_wal_upgrade_checkpoints_committed_rows_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wal.sqlite3'
            db = sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE retained(value TEXT)')
            with db:
                db.execute('INSERT INTO retained VALUES(?)', ('synthetic-data',))
            self.assertGreater(Path(str(path) + '-wal').stat().st_size, 0)
            configure_storage(db, path)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(db.execute('SELECT value FROM retained').fetchone()[0], 'synthetic-data')
            self.assertFalse(Path(str(path) + '-wal').exists())
            db.close()

    def test_active_reader_blocks_mode_change_with_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'busy.sqlite3'
            db = sqlite3.connect(path, timeout=.01)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE retained(value TEXT)')
            with db:
                db.execute("INSERT INTO retained VALUES('first')")
            reader = sqlite3.connect(path)
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM retained').fetchall()
            with db:
                db.execute("INSERT INTO retained VALUES('second')")
            try:
                with self.assertRaisesRegex(ValueError, 'history_migration_busy'):
                    configure_storage(db, path)
                self.assertEqual(db.execute('SELECT count(*) FROM retained').fetchone()[0], 2)
            finally:
                reader.rollback()
                reader.close()
                db.close()


if __name__ == '__main__':
    unittest.main()
