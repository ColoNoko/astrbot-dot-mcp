"""Removing the per-message key quota does not remove dedupe/storage guards."""
from pathlib import Path
import tempfile
import unittest

from test_host import BINDING, Event, Plain, Rejected, Store, snapshot


class ReplyNoQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'inbox.sqlite3', BINDING)
        self.mid, _ = self.store.ingest(snapshot(Event(), BINDING, Plain))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_more_than_eight_distinct_keys_same_key_dedupe_and_conflict(self):
        for number in range(16):
            self.assertEqual(self.store.claim(self.mid, 'reply', f'key-{number}'), ('sending', True))
        self.assertEqual(self.store.claim(self.mid, 'reply', 'key-0'), ('sending', False))
        with self.assertRaisesRegex(Rejected, 'reply_key_conflict'):
            self.store.claim(self.mid, 'different', 'key-0')
        value = snapshot(Event(), BINDING, Plain)
        value['source_id'] = 'another-message'
        other, _ = self.store.ingest(value)
        with self.assertRaisesRegex(Rejected, 'reply_key_conflict'):
            self.store.claim(other, 'reply', 'key-0')

    def test_storage_admission_is_still_required_after_eighth_key(self):
        for number in range(8):
            self.store.claim(self.mid, 'reply', f'key-{number}')
        called = []
        def full(message_id, text, reply_key):
            called.append((message_id, text, reply_key))
            raise Rejected('storage_full')
        self.store.history.admit_reply = full
        with self.assertRaisesRegex(Rejected, 'storage_full'):
            self.store.claim(self.mid, 'reply', 'key-8')
        self.assertEqual(called, [(self.mid, 'reply', 'key-8')])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies').fetchone()[0], 8)
        self.assertEqual(self.store.claim(self.mid, 'reply', 'key-0'), ('sending', False))


if __name__ == '__main__': unittest.main()
