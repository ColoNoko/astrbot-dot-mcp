"""Offline bounded rejection diagnostics; no QQ adapter, NAS, or callbacks."""
import errno
import importlib
import json
import os
from pathlib import Path
import stat
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
alerts = importlib.import_module('astrbot_dot_plugin.capacity_alert')


class CapacityAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'private'
        self.data.mkdir(mode=0o700)
        self.path = self.data / alerts.FILENAME
        self.temporary = self.data / alerts._TEMP_FILENAME
        self.alert = alerts.CapacityAlert(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value, *, mode=0o600):
        self.path.write_text(json.dumps(value))
        self.path.chmod(mode)

    def sample(self, **values):
        return {'active': True, 'code': 'storage_full', 'count': 1,
                'last_utc_ms': 123456, **values}

    def assert_memory_only(self, status):
        self.assertTrue(status['active'])
        self.assertFalse(status['persistent'])
        self.assertEqual(status['count'], 1)
        self.assertEqual(status['code'], 'storage_full')

    def assert_no_temporary(self):
        self.assertFalse(self.temporary.exists())
        self.assertEqual({entry.name for entry in self.data.iterdir()},
                         {alerts.FILENAME} if self.path.exists() else set())

    def test_initial_empty_and_success_without_failure_do_not_write(self):
        expected = {'active': False, 'code': None, 'count': 0,
                    'last_utc_ms': None, 'persistent': False}
        self.assertEqual(self.alert.status(), expected)
        self.assertEqual(self.alert.note_success(), expected)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_record_restart_clear_and_fixed_bound(self):
        with patch.object(alerts.time, 'time_ns', return_value=123456_000000):
            status = self.alert.note_rejection('storage_full')
        self.assertEqual(status, {**self.sample(), 'persistent': True})
        self.assertEqual(self.path.stat().st_size, 512)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text()), self.sample())
        self.assert_no_temporary()
        restarted = alerts.CapacityAlert(self.data)
        self.assertEqual(restarted.status(), status)
        cleared = restarted.note_success()
        self.assertEqual(cleared, {**status, 'active': False})
        self.assertEqual(alerts.CapacityAlert(self.data).status(), cleared)
        with patch.object(alerts.os, 'replace', side_effect=AssertionError('unexpected write')):
            self.assertEqual(restarted.note_success(), cleared)
        with patch.object(alerts.time, 'time_ns', return_value=123457_000000):
            result = restarted.note_rejection('storage_busy')
        self.assertEqual(result, {**self.sample(count=2, code='storage_busy', last_utc_ms=123457),
                                  'persistent': True})
        self.assertEqual(self.path.stat().st_size, 512)

    def test_unknown_codes_never_retain_exception_or_private_input(self):
        private = 'QQ=12345 source=private-source https://secret.example/ message=private'
        class PrivateException(Exception):
            def __str__(self):
                raise AssertionError('Exception contents must never be read')
        for code in (private, None, [], {}, True, PrivateException(private)):
            result = self.alert.note_rejection(code)
            self.assertEqual(result['code'], 'ingest_failed')
            self.assertNotIn(private, json.dumps(result))
            self.assertNotIn(private, self.path.read_text())
        for code in ('storage_full', 'storage_busy', 'ingest_failed'):
            self.assertEqual(self.alert.note_rejection(code)['code'], code)
        self.assertEqual(set(json.loads(self.path.read_text())),
                         {'active', 'code', 'count', 'last_utc_ms'})

    def test_count_saturates_time_bounded_and_status_is_detached(self):
        self.write(self.sample(count=alerts.MAX_COUNT))
        alert = alerts.CapacityAlert(self.data)
        for value, expected in ((-1, 0), (10**30, alerts.MAX_UTC_MS)):
            with patch.object(alerts.time, 'time_ns', return_value=value):
                status = alert.note_rejection('storage_busy')
            self.assertEqual(status['count'], alerts.MAX_COUNT)
            self.assertEqual(status['last_utc_ms'], expected)
            status.update(active=False, code='private', count=-1)
            self.assertTrue(alert.status()['active'])
            self.assertEqual(alert.status()['code'], 'storage_busy')
            self.assertEqual(alert.status()['count'], alerts.MAX_COUNT)
        self.assertEqual(self.path.stat().st_size, 512)

    def test_creation_disk_full_falls_back_to_memory_and_recovers(self):
        original = alerts.os.open
        def full(path, *args, **kwargs):
            if path == alerts._TEMP_FILENAME:
                raise OSError(errno.ENOSPC, 'private filesystem exception text')
            return original(path, *args, **kwargs)
        with patch.object(alerts.os, 'open', side_effect=full):
            self.assert_memory_only(self.alert.note_rejection('storage_full'))
        self.assertEqual(list(self.data.iterdir()), [])
        result = self.alert.note_rejection('storage_busy')
        self.assertTrue(result['persistent'])
        self.assertEqual(result['count'], 2)
        self.assertEqual(alerts.CapacityAlert(self.data).status(), result)

    def test_write_disk_full_keeps_previous_file_and_memory_current(self):
        old = self.alert.note_rejection('storage_busy')
        previous_bytes = self.path.read_bytes()
        with patch.object(alerts.os, 'write', side_effect=OSError(errno.ENOSPC, 'disk full')):
            result = self.alert.note_rejection('storage_full')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['code'], 'storage_full')
        self.assertFalse(result['persistent'])
        self.assertEqual(self.path.read_bytes(), previous_bytes)
        self.assertEqual(alerts.CapacityAlert(self.data).status(), old)
        self.assert_no_temporary()

    def test_partial_write_then_failure_does_not_replace_previous_state(self):
        old = self.alert.note_rejection('storage_busy')
        original = alerts.os.write
        calls = []
        def fail_after_partial(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return original(fd, data[:19])
            raise OSError(errno.ENOSPC, 'full')
        with patch.object(alerts.os, 'write', side_effect=fail_after_partial):
            result = self.alert.note_rejection('storage_full')
        self.assertFalse(result['persistent'])
        self.assertEqual(calls, [512, 493])
        self.assertEqual(alerts.CapacityAlert(self.data).status(), old)
        self.assert_no_temporary()

    def test_short_writes_are_completed(self):
        original = alerts.os.write
        with patch.object(alerts.os, 'write', side_effect=lambda fd, data: original(fd, data[:7])):
            result = self.alert.note_rejection('storage_full')
        self.assertTrue(result['persistent'])
        self.assertEqual(alerts.CapacityAlert(self.data).status(), result)
        self.assertEqual(self.path.stat().st_size, 512)
        self.assert_no_temporary()

    def test_zero_write_fsync_and_rename_failures_cleanup(self):
        for operation, effect in (('write', lambda *_: 0),
                                  ('fsync', OSError(errno.EIO, 'fsync failed')),
                                  ('replace', OSError(errno.EACCES, 'rename failed'))):
            with self.subTest(operation=operation):
                self.alert = alerts.CapacityAlert(self.data)
                with patch.object(alerts.os, operation, side_effect=effect):
                    self.assert_memory_only(self.alert.note_rejection('storage_full'))
                self.assertEqual(list(self.data.iterdir()), [])

    def test_directory_fsync_failure_does_not_claim_durable_write(self):
        original = alerts.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'directory sync failed')
            return original(fd)
        with patch.object(alerts.os, 'fsync', side_effect=fail_directory):
            result = self.alert.note_rejection('storage_full')
        self.assertFalse(result['persistent'])
        self.assertTrue(self.path.exists())
        self.assert_no_temporary()

    def test_failed_clear_is_memory_only_and_retried_on_next_success(self):
        old = self.alert.note_rejection('storage_full')
        with patch.object(alerts.os, 'replace', side_effect=OSError(errno.ENOSPC, 'full')):
            cleared = self.alert.note_success()
        self.assertEqual(cleared, {**old, 'active': False, 'persistent': False})
        self.assertEqual(alerts.CapacityAlert(self.data).status(), old)
        self.assert_no_temporary()
        result = self.alert.note_success()
        self.assertEqual(result, {**cleared, 'persistent': True})
        self.assertEqual(alerts.CapacityAlert(self.data).status(), result)

    def test_malformed_schema_is_not_loaded_or_overwritten(self):
        samples = [b'', b'{', b'[]', b'null', b'x' * 513, b'\xff',
                   b'{"active":true,"active":false,"code":"storage_full","count":1,"last_utc_ms":1}']
        for value in (self.sample(extra='private'), self.sample(active=1), self.sample(count=True),
                      self.sample(count=-1), self.sample(count=alerts.MAX_COUNT + 1),
                      self.sample(code='private'), self.sample(last_utc_ms=True),
                      self.sample(last_utc_ms=-1), self.sample(last_utc_ms=alerts.MAX_UTC_MS + 1),
                      self.sample(last_utc_ms=float('nan')), self.sample(last_utc_ms=None),
                      self.sample(count=0), self.sample(code=None), {'count': 1}):
            samples.append(json.dumps(value).encode())
        for raw in samples:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.path.chmod(0o600)
                alert = alerts.CapacityAlert(self.data)
                self.assertEqual(alert.status()['count'], 0)
                self.assertFalse(alert.status()['persistent'])
                self.assert_memory_only(alert.note_rejection('storage_full'))
                self.assertEqual(self.path.read_bytes(), raw)
                self.assert_no_temporary()

    def test_valid_empty_file_is_accepted(self):
        self.write({'active': False, 'code': None, 'count': 0, 'last_utc_ms': None})
        result = alerts.CapacityAlert(self.data).status()
        self.assertTrue(result['persistent'])
        self.assertEqual(result['count'], 0)

    def test_symlink_dangling_symlink_hardlink_and_fifo_are_not_overwritten(self):
        target = self.root / 'target'
        target.write_text(json.dumps(self.sample()))
        target.chmod(0o600)
        original = target.read_bytes()
        variants = (
            lambda: self.path.symlink_to(target),
            lambda: self.path.symlink_to(self.root / 'absent'),
            lambda: os.link(target, self.path),
            lambda: os.mkfifo(self.path, 0o600),
        )
        for create in variants:
            with self.subTest(create=create):
                create()
                before = self.path.lstat()
                alert = alerts.CapacityAlert(self.data)
                self.assert_memory_only(alert.note_rejection('storage_full'))
                after = self.path.lstat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode),
                                 (after.st_dev, after.st_ino, after.st_mode))
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(self.temporary.exists())
                self.path.unlink()

    def test_unsafe_file_permissions_or_owner_are_not_changed(self):
        self.write(self.sample(), mode=0o644)
        original = self.path.read_bytes()
        self.assert_memory_only(alerts.CapacityAlert(self.data).note_rejection('storage_full'))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.chmod(0o600)
        with patch.object(alerts.os, 'getuid', return_value=os.getuid() + 1):
            self.assert_memory_only(alerts.CapacityAlert(self.data).note_rejection('storage_full'))
        self.assertEqual(self.path.read_bytes(), original)

    def test_existing_permissions_preserved_despite_umask(self):
        self.write(self.sample(), mode=0o640)
        # Unsafe group access fails closed rather than repairing permissions.
        self.assert_memory_only(alerts.CapacityAlert(self.data).note_rejection('storage_full'))
        self.path.chmod(0o600)
        old_umask = os.umask(0o777)
        try:
            result = alerts.CapacityAlert(self.data).note_rejection('storage_full')
        finally:
            os.umask(old_umask)
        self.assertTrue(result['persistent'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_new_file_is_readable_private_despite_umask(self):
        old_umask = os.umask(0o777)
        try:
            result = self.alert.note_rejection('storage_full')
        finally:
            os.umask(old_umask)
        self.assertTrue(result['persistent'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(alerts.CapacityAlert(self.data).status(), result)

    def test_success_uses_atomic_replacement_without_truncating_previous_inode(self):
        self.alert.note_rejection('storage_busy')
        with self.path.open('rb') as reader:
            old = reader.read()
            before = os.fstat(reader.fileno())
            result = self.alert.note_rejection('storage_full')
            reader.seek(0)
            self.assertEqual(reader.read(), old)
        self.assertTrue(result['persistent'])
        self.assertNotEqual(self.path.stat().st_ino, before.st_ino)
        self.assertEqual(alerts.CapacityAlert(self.data).status(), result)

    def test_existing_temp_is_untouched_and_storage_remains_bounded(self):
        sentinel = b'not our temporary file'
        self.temporary.write_bytes(sentinel)
        self.temporary.chmod(0o600)
        for _ in range(5):
            result = self.alert.note_rejection('storage_full')
            self.assertFalse(result['persistent'])
        self.assertEqual(result['count'], 5)
        self.assertEqual(self.temporary.read_bytes(), sentinel)
        self.assertEqual(list(self.data.iterdir()), [self.temporary])

    def test_missing_unsafe_and_symlinked_directory_not_created_or_changed(self):
        missing = self.root / 'missing'
        self.assert_memory_only(alerts.CapacityAlert(missing).note_rejection('storage_full'))
        self.assertFalse(missing.exists())
        self.data.chmod(0o755)
        self.assert_memory_only(alerts.CapacityAlert(self.data).note_rejection('storage_full'))
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o755)
        self.data.chmod(0o700)
        link = self.root / 'linked'
        link.symlink_to(self.data, target_is_directory=True)
        self.assert_memory_only(alerts.CapacityAlert(link).note_rejection('storage_full'))
        nested = self.data / 'nested'
        nested.mkdir(mode=0o700)
        self.assert_memory_only(alerts.CapacityAlert(link / 'nested').note_rejection('storage_full'))
        self.assertEqual(list(nested.iterdir()), [])

    def test_target_replaced_during_write_is_not_overwritten(self):
        original = alerts.os.fsync
        private = self.root / 'external'
        private.write_bytes(b'unchanged')
        def swap_on_sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                self.path.symlink_to(private)
            return original(fd)
        with patch.object(alerts.os, 'fsync', side_effect=swap_on_sync):
            self.assert_memory_only(self.alert.note_rejection('storage_full'))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(private.read_bytes(), b'unchanged')
        self.assertFalse(self.temporary.exists())

    def test_temporary_replaced_during_write_is_not_published_or_removed(self):
        original = alerts.os.fsync
        external = self.root / 'external'
        external.write_bytes(b'unchanged')
        def swap_on_sync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                self.temporary.unlink()
                self.temporary.symlink_to(external)
            return original(fd)
        with patch.object(alerts.os, 'fsync', side_effect=swap_on_sync):
            self.assert_memory_only(self.alert.note_rejection('storage_full'))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.temporary.is_symlink())
        self.assertEqual(external.read_bytes(), b'unchanged')

    def test_cleanup_failure_leaves_only_one_bounded_temporary(self):
        with patch.object(alerts.os, 'replace', side_effect=OSError(errno.EIO, 'failed')):
            with patch.object(alerts.os, 'unlink', side_effect=OSError(errno.EIO, 'failed')):
                self.assert_memory_only(self.alert.note_rejection('storage_full'))
        for _ in range(5):
            self.assertFalse(self.alert.note_rejection('storage_full')['persistent'])
        self.assertEqual(list(self.data.iterdir()), [self.temporary])
        self.assertEqual(self.temporary.stat().st_size, 512)


if __name__ == '__main__':
    unittest.main()
