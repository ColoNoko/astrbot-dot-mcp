"""Bounded attachment-response observations with no payload or routing content.

UTC is a display label; only this process's monotonic clock measures intervals.
The ``http_write_eof`` span observes local write completion, not remote receipt.
No network request, background task, or persistent state is created here.
"""
from collections import deque
import json
import logging
import re
import time
import uuid

MAX_SPANS = 12
MAX_MS = 2147483647
MAX_STAMP_MS = 253402300800000
MAX_ENCODED_BYTES = 32 * 1024 * 1024
MAX_LOGS_PER_MINUTE = 60
PHASES = frozenset(('main_guard', 'main_scope', 'main_json_budget', 'dispatch',
                    'json_encode', 'http_prepare', 'http_write', 'http_write_eof', 'complete'))
OUTCOMES = frozenset(('succeeded', 'failed', 'cancelled'))
ERRORS = frozenset(('cancelled', 'timeout', 'connection_failed',
                    'serialization_failed', 'write_failed', 'dispatch_failed'))
_SCOPES = frozenset(('main', 'endpoint'))
_TIMEOUT_KINDS = frozenset(('total', 'connect', 'read_idle', 'response_headers', 'unknown'))
_MESSAGE_ID = re.compile(r'msg_[a-f0-9]{32}\Z')
_READ_ID = re.compile(r'read_[a-f0-9]{32}\Z')
_RESPONSE_ID = re.compile(r'response_[a-f0-9]{32}\Z')


def _enum(value, choices):
    return value if type(value) is str and value in choices else None


def _number(value, maximum=MAX_MS):
    return value if type(value) is int and 0 <= value <= maximum else None


def _identifier(value, pattern):
    return value if type(value) is str and pattern.fullmatch(value) is not None else None


def _stamp(value):
    if type(value) is not dict:
        return None
    utc = _number(value.get('utc_ms'), MAX_STAMP_MS)
    monotonic = _number(value.get('monotonic_ms'), MAX_STAMP_MS)
    if utc is None or monotonic is None:
        return None
    return {'utc_ms': utc, 'monotonic_ms': monotonic}


def _elapsed(started, finished):
    if started is None or finished is None:
        return None
    elapsed = finished['monotonic_ms'] - started['monotonic_ms']
    return min(MAX_MS, elapsed) if elapsed >= 0 else None


def _download(value):
    if type(value) is not dict:
        return None
    result = {}
    for key in ('content_length_bytes', 'bytes_received', 'last_progress_gap_ms', 'elapsed_ms'):
        number = _number(value.get(key))
        if number is not None:
            result[key] = number
    kind = _enum(value.get('timeout_kind'), _TIMEOUT_KINDS)
    if kind is not None:
        result['timeout_kind'] = kind
    return result or None


class ResponseTimingLogger:
    """Share one helper between Main and Endpoint for a 60-lines/minute cap.

    ``logger`` is AstrBot's native logger (or an ordinary Python logger).
    Failures in logging are deliberately isolated from response delivery.
    """
    def __init__(self, logger, *, clock=None):
        self.logger = logger
        self.clock = clock or time.monotonic
        self._recent = deque(maxlen=MAX_LOGS_PER_MINUTE)

    def __call__(self, snapshot):
        safe = ResponseTrace.sanitize(snapshot)
        if safe is None or safe['outcome'] not in OUTCOMES:
            return False
        try:
            now = self.clock()
            # A local monotonic clock cannot legitimately reverse or be NaN.
            if (type(now) not in (int, float) or not 0 <= now < float('inf') or
                    (self._recent and now < self._recent[-1])):
                return False
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) >= MAX_LOGS_PER_MINUTE:
                return False
            self._recent.append(now)
            self.logger.info('dot_private_attachment_response ' + json.dumps(
                safe, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
            return True
        except Exception:
            return False


class ResponseTrace:
    """One request's small, synchronous timeline; terminal snapshots are frozen.

    Call ``mark`` before each measured phase, then ``finish`` exactly once in a
    finally block. ``logger`` can be a shared ResponseTimingLogger or a callback
    accepting the sanitized completion snapshot. It never receives raw input.
    """
    def __init__(self, scope, message_id=None, *, read_id=None, logger=None, stamp=None):
        if _enum(scope, _SCOPES) is None:
            raise ValueError('invalid_response_scope')
        self.scope = scope
        self.message_id = _identifier(message_id, _MESSAGE_ID)
        self.read_id = _identifier(read_id, _READ_ID)
        self.response_id = 'response_' + uuid.uuid4().hex
        self._clock = stamp or self.stamp
        self._logger = _DEFAULT_LOGGER if logger is None else logger
        self.started = self._now()
        self.finished = None
        self.outcome = 'running'
        self.error = None
        self.encoded_bytes = None
        self.written_bytes = None
        self._last_write = None
        self.download = None
        self._phase = None
        self._phase_started = None
        self._spans = []

    @staticmethod
    def stamp():
        return time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000

    def _now(self):
        value = self._clock()
        if type(value) is not tuple or len(value) != 2:
            return None
        return _stamp({'utc_ms': value[0], 'monotonic_ms': value[1]})

    def _span(self, now):
        return {'phase': self._phase, 'started': self._phase_started,
                'finished': now, 'elapsed_ms': _elapsed(self._phase_started, now)}

    def _append(self, span):
        self._spans.append(span)
        del self._spans[:-MAX_SPANS]

    def mark(self, phase):
        if (self.outcome != 'running' or _enum(phase, PHASES) is None or
                phase == self._phase):
            return
        now = self._now()
        if self._phase is not None:
            self._append(self._span(now))
        self._phase, self._phase_started = phase, now

    def bind_read_id(self, read_id):
        value = _identifier(read_id, _READ_ID)
        if self.outcome == 'running' and self.read_id is None and value is not None:
            self.read_id = value

    def set_encoded_bytes(self, value):
        number = _number(value, MAX_ENCODED_BYTES)
        if self.outcome == 'running' and number is not None:
            self.encoded_bytes = number

    def note_write_progress(self, value):
        number = _number(value, MAX_ENCODED_BYTES)
        if (self.outcome == 'running' and number is not None and
                (self.written_bytes is None or number > self.written_bytes)):
            self.written_bytes = number
            self._last_write = self._now()

    def set_download(self, snapshot):
        if self.outcome == 'running' and type(snapshot) is dict:
            self.bind_read_id(snapshot.get('read_id'))
            self.download = _download(snapshot)

    def snapshot(self):
        now = self.finished if self.outcome != 'running' else self._now()
        spans = list(self._spans)
        if self.outcome == 'running' and self._phase is not None:
            spans.append(self._span(now))
        return self.sanitize({'response_id': self.response_id, 'scope': self.scope,
            'message_id': self.message_id, 'read_id': self.read_id,
            'outcome': self.outcome, 'error': self.error, 'started': self.started,
            'finished': self.finished, 'elapsed_ms': _elapsed(self.started, now),
            'encoded_bytes': self.encoded_bytes, 'download': self.download,
            'written_bytes': self.written_bytes,
            'last_write_progress_gap_ms': _elapsed(self._last_write, now),
            'spans': spans[-MAX_SPANS:]})

    def finish(self, outcome='succeeded', error=None):
        if self.outcome == 'running':
            now = self._now()
            if self._phase is not None:
                self._append(self._span(now))
            if self._phase != 'complete':
                self._append({'phase': 'complete', 'started': now,
                              'finished': now, 'elapsed_ms': 0 if now is not None else None})
            self.outcome = _enum(outcome, OUTCOMES) or 'failed'
            self.error = ('cancelled' if self.outcome == 'cancelled' else
                          _enum(error, ERRORS) if self.outcome == 'failed' else None)
            self.finished = now
            try:
                self._logger(self.snapshot())
            except Exception:
                pass
        return self.snapshot()

    @staticmethod
    def sanitize(snapshot):
        """Copy only allowlisted fields; safe for lifecycle/status aggregation."""
        if type(snapshot) is not dict:
            return None
        response_id = _identifier(snapshot.get('response_id'), _RESPONSE_ID)
        scope = _enum(snapshot.get('scope'), _SCOPES)
        outcome = _enum(snapshot.get('outcome'), OUTCOMES | {'running'})
        if response_id is None or scope is None or outcome is None:
            return None
        started, finished = _stamp(snapshot.get('started')), _stamp(snapshot.get('finished'))
        spans = []
        raw_spans = snapshot.get('spans')
        if type(raw_spans) is list:
            for span in raw_spans[-MAX_SPANS:]:
                if type(span) is not dict or _enum(span.get('phase'), PHASES) is None:
                    continue
                start, end = _stamp(span.get('started')), _stamp(span.get('finished'))
                spans.append({'phase': span['phase'], 'started': start, 'finished': end,
                              'elapsed_ms': _elapsed(start, end)})
        result = {'response_id': response_id, 'scope': scope,
            'message_id': _identifier(snapshot.get('message_id'), _MESSAGE_ID),
            'read_id': _identifier(snapshot.get('read_id'), _READ_ID),
            'outcome': outcome,
            'error': ('cancelled' if outcome == 'cancelled' else
                      _enum(snapshot.get('error'), ERRORS) if outcome == 'failed' else None),
            'started': started, 'finished': finished,
            'elapsed_ms': (_number(snapshot.get('elapsed_ms')) if outcome == 'running'
                           else _elapsed(started, finished)),
            'encoded_bytes': _number(snapshot.get('encoded_bytes'), MAX_ENCODED_BYTES),
            'written_bytes': _number(snapshot.get('written_bytes'), MAX_ENCODED_BYTES),
            'last_write_progress_gap_ms': _number(snapshot.get('last_write_progress_gap_ms')),
            'spans': spans}
        download = _download(snapshot.get('download'))
        if download is not None:
            result['download'] = download
        return result


_DEFAULT_LOGGER = ResponseTimingLogger(logging.getLogger(__name__))
