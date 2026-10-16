"""Bounded admission-failure diagnostics, independent of the message database.

Only fixed error codes, a counter, and a UTC display timestamp are retained.
A write failure leaves the latest snapshot in memory with persistent=False.
This module never creates directories, changes their permissions, or logs input.
"""
import json
import os
from pathlib import Path
import stat
import threading
import time

FILENAME = 'capacity_alert.json'
_TEMP_FILENAME = '.capacity_alert.json.tmp'
MAX_FILE_BYTES = 512
MAX_COUNT = 2147483647
MAX_UTC_MS = 253402300799999
_CODES = frozenset(('storage_full', 'storage_busy', 'ingest_failed'))
_FIELDS = frozenset(('active', 'code', 'count', 'last_utc_ms'))


def _empty():
    return {'active': False, 'code': None, 'count': 0, 'last_utc_ms': None}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('capacity_alert_invalid')
        result[key] = value
    return result


def _validate(value):
    if (type(value) is not dict or value.keys() != _FIELDS or
            type(value['active']) is not bool or type(value['count']) is not int or
            not 0 <= value['count'] <= MAX_COUNT):
        raise ValueError('capacity_alert_invalid')
    if value['count'] == 0:
        if value != _empty():
            raise ValueError('capacity_alert_invalid')
    elif (type(value['code']) is not str or value['code'] not in _CODES or
          type(value['last_utc_ms']) is not int or
          not 0 <= value['last_utc_ms'] <= MAX_UTC_MS):
        raise ValueError('capacity_alert_invalid')
    return value


def _private_directory(path):
    """Pin existing components, never following a directory symlink."""
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('capacity_alert_unsafe')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _safe_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
            not 0 < info.st_size <= MAX_FILE_BYTES):
        raise ValueError('capacity_alert_unsafe')


def _identity(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stat_file(directory):
    try:
        return os.stat(FILENAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read_file(directory):
    # Stat first so a dangling symlink is never mistaken for an absent file.
    before = _stat_file(directory)
    if before is None:
        return None, None
    _safe_file(before)
    descriptor = os.open(FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        _safe_file(info)
        if _identity(info) != _identity(before):
            raise ValueError('capacity_alert_changed')
        raw = os.read(descriptor, MAX_FILE_BYTES + 1)
        if (len(raw) != info.st_size or len(raw) > MAX_FILE_BYTES or
                _identity(os.fstat(descriptor)) != _identity(info) or
                _identity(_stat_file(directory)) != _identity(info)):
            raise ValueError('capacity_alert_changed')
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_strict_object)
        return _validate(value), info
    finally:
        os.close(descriptor)


class CapacityAlert:
    """One process-owned diagnostic; callers supply only an error code.

    A fixed, exclusive temporary name bounds disk use during atomic replacement.
    An existing temporary file is never overwritten or removed by a new instance;
    a stale file after abrupt termination therefore fails safely to memory.
    """
    def __init__(self, data_dir):
        self._directory = Path(os.path.abspath(data_dir))
        self._lock = threading.Lock()
        self._value = _empty()
        self._persistent = False
        directory = None
        try:
            directory = _private_directory(self._directory)
            value, _ = _read_file(directory)
            if value is not None:
                self._value = value
                self._persistent = True
        except (OSError, ValueError):
            pass
        finally:
            if directory is not None:
                os.close(directory)

    def status(self):
        with self._lock:
            return self._snapshot()

    def _snapshot(self):
        return {**self._value, 'persistent': self._persistent}

    def note_rejection(self, code):
        """Record one failed admission, without inspecting exception text."""
        with self._lock:
            self._value = {
                'active': True,
                'code': code if type(code) is str and code in _CODES else 'ingest_failed',
                'count': min(MAX_COUNT, self._value['count'] + 1),
                'last_utc_ms': min(MAX_UTC_MS, max(0, time.time_ns() // 1_000_000)),
            }
            self._persist()
            return self._snapshot()

    def note_success(self):
        """Clear an active alert only after successful admission; retain history."""
        with self._lock:
            if self._value['active'] or (self._value['count'] and not self._persistent):
                self._value['active'] = False
                self._persist()
            return self._snapshot()

    def _persist(self):
        self._persistent = False
        directory = descriptor = None
        temporary_created = False
        temporary_inode = None
        try:
            directory = _private_directory(self._directory)
            _, previous = _read_file(directory)
            mode = stat.S_IMODE(previous.st_mode) if previous is not None else 0o600
            raw = json.dumps(self._value, separators=(',', ':'), ensure_ascii=True).encode('ascii')
            if len(raw) >= MAX_FILE_BYTES:
                raise ValueError('capacity_alert_invalid')
            raw = raw.ljust(MAX_FILE_BYTES - 1, b' ') + b'\n'
            descriptor = os.open(_TEMP_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 mode, dir_fd=directory)
            temporary_created = True
            created = os.fstat(descriptor)
            temporary_inode = (created.st_dev, created.st_ino)
            # Atomic replacement must preserve the existing file's permission bits,
            # or use 0600 for a new file, regardless of the process umask.
            # No existing inode is chmodded.
            if stat.S_IMODE(created.st_mode) != mode:
                os.fchmod(descriptor, mode)
            remaining = memoryview(raw)
            while remaining:
                written = os.write(descriptor, remaining)
                if not written:
                    raise OSError('capacity_alert_write_failed')
                remaining = remaining[written:]
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            _safe_file(info)
            if info.st_size != MAX_FILE_BYTES:
                raise ValueError('capacity_alert_invalid')
            os.close(descriptor)
            descriptor = None
            if (_identity(_stat_file(directory)) != _identity(previous) or
                    _identity(os.stat(_TEMP_FILENAME, dir_fd=directory, follow_symlinks=False)) !=
                    _identity(info)):
                raise ValueError('capacity_alert_changed')
            os.replace(_TEMP_FILENAME, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
            temporary_created = False
            os.fsync(directory)
            self._persistent = True
        except (OSError, ValueError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_created:
                try:
                    current = os.stat(_TEMP_FILENAME, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == temporary_inode:
                        os.unlink(_TEMP_FILENAME, dir_fd=directory)
                except OSError:
                    pass
            if directory is not None:
                os.close(directory)
