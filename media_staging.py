"""Bounded, private staging for generated media carried by qq_reply envelopes.

This is deliberately not a file service: callers supply bytes, never paths,
URLs or recipients. The host supplies a synchronous, current reactive-target
guard. All methods run on its event-loop thread; no staging operation awaits
before a guarded write. Only commit invokes the existing host reply path.

The idle deadline is 20 seconds of *actual new-byte* inactivity, including the
gap after begin. Duplicated begin/chunk requests do not extend it. A 20 MB file
needs 77 maximum-sized chunks, so a tool orchestrator that pauses for 20 seconds
cannot reliably complete that upload. There is no hidden keepalive exception.
"""

import asyncio
import base64
import binascii
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import inspect
import re
import tempfile
import time
import uuid

from .host import Rejected
from .attachments import sniff_mime
from .media_reply import MAX_MEDIA_BYTES, MediaReply, _IMAGE_MIMES, _filename


MAX_CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BASE64_CHARS = 4 * ((MAX_CHUNK_BYTES + 2) // 3)
MAX_ACTIVE_UPLOADS = 4
MAX_RESERVED_BYTES = 40_000_000
MAX_CHUNKS = 4096
MAX_FINISHED_UPLOADS = 64
IDLE_SECONDS = 20
UPLOAD_ID = re.compile(r'upl_[a-f0-9]{32}\Z')
_MESSAGE_ID = re.compile(r'msg_[a-f0-9]{32}\Z')
_REPLY_KEY = re.compile(r'[a-zA-Z0-9_.:-]{1,128}\Z')
_SHA256 = re.compile(r'[a-f0-9]{64}\Z')
_RESULT_STATES = frozenset(('sending', 'adapter_completed', 'uncertain', 'not_sent'))

STAGING_ERRORS = frozenset((
    'invalid_media_upload', 'invalid_media_size', 'invalid_media_sha256',
    'invalid_media_chunk', 'media_upload_not_found',
    'media_upload_binding_mismatch', 'media_upload_conflict',
    'media_upload_limit', 'media_upload_expired', 'media_upload_incomplete',
    'media_upload_hash_mismatch', 'media_upload_offset_conflict',
    'media_upload_chunk_limit', 'media_upload_closed', 'media_upload_busy',
    'media_upload_storage_failed', 'media_upload_aborted',
    'media_upload_finished',
))


@dataclass
class _Upload:
    upload_id: str
    message_id: str
    reply_key: str
    kind: str
    filename: str
    size_bytes: int
    sha256: str
    last_progress: float
    stream: object = field(repr=False)
    offset: int = 0
    state: str = 'uploading'
    chunks: dict = field(default_factory=dict, repr=False)
    result: object = field(default=None, repr=False)
    error: str = ''
    reserved: bool = True

    @property
    def identity(self):
        return (self.kind, self.filename, self.size_bytes, self.sha256)


class MediaStaging:
    """One host's ephemeral upload collection, with no adapter of its own.

    ``guard(message_id)`` must synchronously check the current host, binding,
    policy and exact captured reactive target. Raising rejects the operation.
    Its authorization must not be inferred from an earlier read or begin.
    Production callers must run on one asyncio event loop. A timer removes
    abandoned files even if no further request arrives. Tests may inject a
    monotonic clock and call expire() directly without an event loop.
    """

    def __init__(self, host, *, guard, clock=time.monotonic):
        if not callable(guard):
            raise TypeError('a synchronous reactive-target guard is required')
        self.host, self.guard, self.clock = host, guard, clock
        self._directory = tempfile.TemporaryDirectory(prefix='dot-media-')
        self._uploads = OrderedDict()
        self._keys = {}
        self._reserved_bytes = 0
        self._active = 0
        self._closed = False
        self._timer = None
        self._committing = False
        self._commit_tasks = set()

    @staticmethod
    def limits():
        return {'max_size_bytes': MAX_MEDIA_BYTES,
                'max_chunk_bytes': MAX_CHUNK_BYTES,
                'max_chunks': MAX_CHUNKS,
                'idle_timeout_seconds': IDLE_SECONDS,
                'max_active_uploads': MAX_ACTIVE_UPLOADS,
                'max_reserved_bytes': MAX_RESERVED_BYTES}

    def status(self):
        self.expire()
        return {'active_uploads': self._active,
                'reserved_bytes': self._reserved_bytes,
                'committing': self._committing,
                'limits': self.limits()}

    def _authorize(self, message_id, reply_key):
        if self._closed:
            raise Rejected('media_upload_closed')
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
            raise Rejected('invalid_message_id')
        if not isinstance(reply_key, str) or not _REPLY_KEY.fullmatch(reply_key):
            raise Rejected('invalid_reply_key')
        if getattr(self.host, 'stopping', False):
            raise Rejected('binding_inactive')
        try:
            result = self.guard(message_id)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise Rejected('binding_inactive')
            if result is False:
                raise Rejected('binding_inactive')
        except Rejected:
            raise
        except Exception:
            raise Rejected('binding_inactive') from None

    def _schedule_expiry(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        deadlines = [item.last_progress + IDLE_SECONDS
                     for item in self._uploads.values() if item.state == 'uploading']
        if not self._closed and deadlines:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._timer = loop.call_later(max(0, min(deadlines) - self.clock()), self.expire)

    def _release(self, item, *, release_reservation=True):
        if item.stream is not None:
            try:
                item.stream.close()
            except OSError:
                pass
            item.stream = None
        item.chunks.clear()
        if item.reserved and release_reservation:
            item.reserved = False
            self._active -= 1
            self._reserved_bytes -= item.size_bytes

    def _finish(self, item, state, *, result=None, error=''):
        self._release(item)
        item.state, item.result, item.error = state, result, error
        terminal = [key for key, value in self._uploads.items()
                    if value.state not in ('uploading', 'committing')]
        for key in terminal[:-MAX_FINISHED_UPLOADS]:
            old = self._uploads.pop(key)
            self._keys.pop((old.message_id, old.reply_key), None)

    def expire(self):
        """Delete idle partial files; duplicate traffic cannot keep them alive."""
        now = self.clock()
        for item in tuple(self._uploads.values()):
            if item.state == 'uploading' and now - item.last_progress >= IDLE_SECONDS:
                self._finish(item, 'expired', error='media_upload_expired')
        self._schedule_expiry()

    @staticmethod
    def _progress(item):
        return {'upload_id': item.upload_id, 'reply_key': item.reply_key,
                'state': item.state, 'next_offset': item.offset,
                'size_bytes': item.size_bytes}

    def _lookup(self, message_id, reply_key, upload_id):
        self.expire()
        self._authorize(message_id, reply_key)
        if not isinstance(upload_id, str) or not UPLOAD_ID.fullmatch(upload_id):
            raise Rejected('invalid_media_upload')
        item = self._uploads.get(upload_id)
        if item is None:
            raise Rejected('media_upload_not_found')
        if (item.message_id, item.reply_key) != (message_id, reply_key):
            raise Rejected('media_upload_binding_mismatch')
        return item

    def begin(self, message_id, reply_key, kind, filename, size_bytes, sha256):
        self.expire()
        self._authorize(message_id, reply_key)
        if type(kind) is not str or kind not in ('image', 'file'):
            raise Rejected('invalid_media_kind')
        filename = _filename(filename)
        if type(size_bytes) is not int or size_bytes <= 0:
            raise Rejected('invalid_media_size')
        if size_bytes > MAX_MEDIA_BYTES:
            raise Rejected('media_too_large')
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise Rejected('invalid_media_sha256')
        key = (message_id, reply_key)
        existing = self._uploads.get(self._keys.get(key))
        if existing is not None:
            if existing.identity != (kind, filename, size_bytes, sha256):
                raise Rejected('media_upload_conflict')
            if existing.error:
                raise Rejected(existing.error)
            return {**self._progress(existing), 'limits': self.limits()}
        if (self._active >= MAX_ACTIVE_UPLOADS or
                self._reserved_bytes + size_bytes > MAX_RESERVED_BYTES):
            raise Rejected('media_upload_limit')
        # No await or user-provided path occurs between this guard and creation.
        self._authorize(message_id, reply_key)
        try:
            stream = tempfile.TemporaryFile(mode='w+b', buffering=0,
                                            dir=self._directory.name)
        except OSError:
            raise Rejected('media_upload_storage_failed') from None
        upload_id = 'upl_' + uuid.uuid4().hex
        item = _Upload(upload_id, message_id, reply_key, kind, filename,
                       size_bytes, sha256, self.clock(), stream)
        self._uploads[upload_id] = item
        self._keys[key] = upload_id
        self._active += 1
        self._reserved_bytes += size_bytes
        self._schedule_expiry()
        return {**self._progress(item), 'limits': self.limits()}

    @staticmethod
    def _decode_chunk(value):
        if (not isinstance(value, str) or not value or
                len(value) > MAX_CHUNK_BASE64_CHARS or len(value) % 4):
            raise Rejected('invalid_media_chunk')
        try:
            data = base64.b64decode(value, validate=True)
            if (not data or len(data) > MAX_CHUNK_BYTES or
                    base64.b64encode(data).decode('ascii') != value):
                raise ValueError()
        except (ValueError, UnicodeError, binascii.Error):
            raise Rejected('invalid_media_chunk') from None
        return data

    def chunk(self, message_id, reply_key, upload_id, offset, data_base64):
        item = self._lookup(message_id, reply_key, upload_id)
        if item.error:
            raise Rejected(item.error)
        if item.state != 'uploading':
            raise Rejected('media_upload_finished' if item.result else 'media_upload_busy')
        if type(offset) is not int or not 0 <= offset < item.size_bytes:
            raise Rejected('media_upload_offset_conflict')
        data = self._decode_chunk(data_base64)
        fingerprint = (len(data), hashlib.sha256(data).digest())
        if offset in item.chunks:
            if item.chunks[offset] != fingerprint:
                raise Rejected('media_upload_offset_conflict')
            return {**self._progress(item), 'duplicate': True}
        if offset != item.offset or offset + len(data) > item.size_bytes:
            raise Rejected('media_upload_offset_conflict')
        if len(item.chunks) >= MAX_CHUNKS:
            raise Rejected('media_upload_chunk_limit')
        # Recheck after parsing/hash work. Revocation never permits a new write.
        try:
            self._authorize(message_id, reply_key)
        except Rejected:
            self._finish(item, 'aborted', error='binding_inactive')
            self._schedule_expiry()
            raise
        if self.clock() - item.last_progress >= IDLE_SECONDS:
            self._finish(item, 'expired', error='media_upload_expired')
            self._schedule_expiry()
            raise Rejected('media_upload_expired')
        try:
            item.stream.seek(offset)
            if item.stream.write(data) != len(data):
                raise OSError()
        except OSError:
            self._finish(item, 'failed', error='media_upload_storage_failed')
            self._schedule_expiry()
            raise Rejected('media_upload_storage_failed') from None
        item.offset += len(data)
        item.chunks[offset] = fingerprint
        item.last_progress = self.clock()
        self._schedule_expiry()
        return {**self._progress(item), 'duplicate': False}

    async def commit(self, message_id, reply_key, upload_id):
        item = self._lookup(message_id, reply_key, upload_id)
        if item.error:
            raise Rejected(item.error)
        if item.result is not None:
            return dict(item.result)
        if item.state != 'uploading' or self._committing:
            raise Rejected('media_upload_busy')
        if item.offset != item.size_bytes:
            raise Rejected('media_upload_incomplete')
        sender = getattr(self.host, 'reply_attachment', None)
        validated_sender = getattr(self.host, 'reply_validated_attachment', None)
        if not callable(sender) and not callable(validated_sender):
            raise Rejected('binding_inactive')
        self._committing = True
        item.state = 'committing'
        task = asyncio.current_task()
        self._commit_tasks.add(task)
        self._schedule_expiry()
        entered_sender = False
        try:
            try:
                item.stream.seek(0)
                data = item.stream.read(item.size_bytes + 1)
            except OSError:
                raise Rejected('media_upload_storage_failed') from None
            if len(data) != item.size_bytes:
                raise Rejected('media_upload_incomplete')
            if hashlib.sha256(data).hexdigest() != item.sha256:
                raise Rejected('media_upload_hash_mismatch')
            mime_type = sniff_mime(data[:512])
            if item.kind == 'image' and mime_type not in _IMAGE_MIMES:
                raise Rejected('unsupported_media_image')
            data_base64 = base64.b64encode(data).decode('ascii')
            del data
            self._authorize(message_id, reply_key)
            # Release the file before awaiting, but count this committing
            # upload against both quotas until it finishes. Exactly one commit
            # holds a full payload; concurrent commits get a fixed error.
            self._release(item, release_reservation=False)
            entered_sender = True
            if callable(validated_sender):
                # Construct only after exact size, digest, canonical encoding,
                # sanitized filename and image magic checks. Main accepts the
                # exact immutable MediaReply type and still owns durable claim.
                media = MediaReply(item.kind, item.filename, data_base64,
                                   item.size_bytes, item.sha256, mime_type)
                answer = await validated_sender(message_id, media, reply_key)
            else:
                answer = await sender(message_id, item.kind, item.filename,
                                      data_base64, reply_key)
            state = answer.get('state') if isinstance(answer, dict) else None
            result = {'reply_key': reply_key,
                      'state': state if state in _RESULT_STATES else 'uncertain'}
            if isinstance(answer, dict) and answer.get('storage_error') in ('storage_full', 'storage_busy'):
                result['storage_error'] = answer['storage_error']
            self._finish(item, result['state'], result=result)
            return dict(result)
        except asyncio.CancelledError:
            result = {'reply_key': reply_key, 'state': 'uncertain' if entered_sender else 'not_sent'}
            self._finish(item, result['state'], result=result)
            raise
        except Rejected as error:
            # Known host rejections happen before adapter entry. Preserve the
            # error, never transparently substitute a different key or payload.
            self._finish(item, 'failed', error=str(error))
            raise
        except Exception:
            if not entered_sender:
                self._finish(item, 'failed', error='media_upload_storage_failed')
                raise Rejected('media_upload_storage_failed') from None
            result = {'reply_key': reply_key, 'state': 'uncertain'}
            self._finish(item, 'uncertain', result=result)
            return dict(result)
        finally:
            self._commit_tasks.discard(task)
            self._committing = False
            self._schedule_expiry()

    def abort(self, message_id, reply_key, upload_id):
        item = self._lookup(message_id, reply_key, upload_id)
        if item.state == 'committing':
            raise Rejected('media_upload_busy')
        if item.state == 'uploading':
            self._finish(item, 'aborted', error='media_upload_aborted')
            self._schedule_expiry()
        return self._progress(item)

    def close(self):
        """Revoke staging, cancel in-flight commits and delete all private files."""
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        for task in tuple(self._commit_tasks):
            task.cancel()
        for item in tuple(self._uploads.values()):
            self._release(item)
        self._uploads.clear()
        self._keys.clear()
        self._directory.cleanup()

    async def wait_closed(self):
        """Drain canceled commits before Main closes their durable reply store."""
        current = asyncio.current_task()
        tasks = tuple(task for task in self._commit_tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
