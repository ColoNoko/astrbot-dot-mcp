"""Ephemeral, bounded lifecycle observations, never message or routing content.

Only this process's monotonic clock measures intervals. UTC timestamps are
display labels, and missing/restarted/evicted observations remain unknown.
The ACK-to-read gap is explicitly unobserved time, not a platform or LLM trace.
"""
from collections import OrderedDict
from copy import deepcopy
import re
import time

MAX_MESSAGES = 32
MAX_OPERATIONS = 8
MAX_MS = 2147483647
_MESSAGE_ID = re.compile(r'msg_[a-f0-9]{32}\Z')
_EVENT_ID = re.compile(r'evt_[a-f0-9]{64}\Z')
_READ_ID = re.compile(r'read_[a-f0-9]{32}\Z')
_STAGES = frozenset(('queued', 'validation', 'source_resolution', 'adapter_api',
    'source_validation', 'dns', 'connection_or_request', 'request_or_response_headers',
    'response_headers', 'response_body', 'cache_io', 'result', 'complete'))
_RPC_TOOLS = frozenset(('qq_read_message', 'qq_read_attachment', 'qq_reply',
    'qq_reply_attachment', 'qq_complete_message'))
_RPC_OUTCOMES = frozenset(('completed', 'tool_error', 'rpc_error', 'cancelled', 'failed'))
_API_ACTIONS = frozenset(('get_image', 'get_file', 'get_group_file_url', 'get_private_file_url'))
_TIMESTAMPS = ('captured', 'ingest_started', 'ingested', 'enqueued',
    'events_transport_started', 'events_transport_finished', 'events_ack', 'first_tool_read')


def _number(value):
    return value if type(value) is int and 0 <= value <= MAX_MS else None


def _stamp(value, latest=None):
    if (type(value) is tuple and len(value) == 2 and
            all(type(v) is int and 0 <= v <= 253402300800000 for v in value) and
            (latest is None or value[1] <= latest[1])):
        return {'utc_ms': value[0], 'monotonic_ms': value[1]}
    return None


def _copied_stamp(value, latest):
    if type(value) is not dict:
        return None
    return _stamp((value.get('utc_ms'), value.get('monotonic_ms')), latest)


def _elapsed(start, end):
    if start is None or end is None:
        return None
    elapsed = end['monotonic_ms'] - start['monotonic_ms']
    return min(MAX_MS, elapsed) if elapsed >= 0 else None


class MessageTiming:
    """One Store-owned ring; methods accept only IDs and allowlisted timings."""
    def __init__(self):
        self._messages = OrderedDict()

    @staticmethod
    def stamp():
        return time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000

    def _record(self, message_id):
        if type(message_id) is not str or _MESSAGE_ID.fullmatch(message_id) is None:
            return None
        if message_id not in self._messages:
            self._messages[message_id] = {
                'message_id': message_id,
                'timestamps': dict.fromkeys(_TIMESTAMPS),
                'event_attempts': [], 'attachment_reads': [], 'reply_attempts': [],
                'rpc_attempts': []}
            while len(self._messages) > MAX_MESSAGES:
                self._messages.popitem(last=False)
        return self._messages[message_id]

    def begin_rpc(self, message_id, tool):
        """Only correlate retained messages; arbitrary RPC IDs cannot fill the ring.

        Called by the HTTP endpoint after authentication, parsing and metadata
        validation. This is not socket arrival or an external platform timestamp.
        """
        if type(tool) is not str or tool not in _RPC_TOOLS:
            return None
        record = self._messages.get(message_id) if type(message_id) is str else None
        if record is None:
            return None
        token = object()
        record['rpc_attempts'].append({'_token': token, 'tool': tool,
            'outcome': 'accepted', 'server_rpc_accepted': _stamp(self.stamp()),
            'processing_started': None, 'processing_finished': None, 'finished': None})
        del record['rpc_attempts'][:-MAX_OPERATIONS]
        return token

    def _rpc(self, message_id, token):
        record = self._messages.get(message_id) if type(message_id) is str else None
        if record is not None:
            return next((row for row in record['rpc_attempts']
                if row['_token'] is token and row['finished'] is None), None)
        return None

    def start_rpc(self, message_id, token):
        operation = self._rpc(message_id, token)
        if operation is not None and operation['processing_started'] is None:
            operation['processing_started'] = _stamp(self.stamp())
            operation['outcome'] = 'processing'

    def finish_rpc(self, message_id, token, outcome):
        operation = self._rpc(message_id, token)
        if operation is not None:
            operation['outcome'] = outcome if type(outcome) is str and outcome in _RPC_OUTCOMES else 'failed'
            operation['finished'] = _stamp(self.stamp())
            if operation['processing_started'] is not None:
                operation['processing_finished'] = operation['finished']

    def note_ingest(self, message_id, *, captured_at=None, ingest_at=None, enqueued_at=None):
        """Called after commit, never for failed admission or duplicate input."""
        record = self._record(message_id)
        if record is None or record['timestamps']['ingested'] is not None:
            return
        now = self.stamp()
        record['timestamps'].update(captured=_stamp(captured_at, now),
            ingest_started=_stamp(ingest_at, now), ingested=_stamp(now),
            enqueued=_stamp(enqueued_at, now))

    def note_read(self, message_id):
        """Only a successful qq_read_message result counts as the first read."""
        record = self._record(message_id)
        if record is not None and record['timestamps']['first_tool_read'] is None:
            record['timestamps']['first_tool_read'] = _stamp(self.stamp())

    def note_event_started(self, message_id, event_id, attempt, stages):
        if (type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None or
                type(attempt) is not int or not 1 <= attempt <= 5 or type(stages) is not dict):
            return
        record = self._record(message_id)
        if record is None:
            return
        existing = next((row for row in record['event_attempts']
                         if row['event_id'] == event_id and row['attempt'] == attempt), None)
        if existing is not None:
            return
        started = _stamp(stages.get('transport_started'), self.stamp())
        record['event_attempts'].append({'event_id': event_id, 'attempt': attempt,
            'outcome': 'sending', 'http_status': None, 'started': started,
            'finished': None, 'ack': None})
        record['event_attempts'] = record['event_attempts'][-MAX_OPERATIONS:]
        if record['timestamps']['events_transport_started'] is None:
            record['timestamps']['events_transport_started'] = started

    def note_event(self, sample):
        """Revalidate a delivery sample; never retain transport dictionaries."""
        if (type(sample) is not dict or type(sample.get('message_id')) is not str or
                _MESSAGE_ID.fullmatch(sample['message_id']) is None or
                type(sample.get('event_id')) is not str or _EVENT_ID.fullmatch(sample['event_id']) is None or
                type(sample.get('attempt')) is not int or not 1 <= sample['attempt'] <= 5):
            return
        timestamps = sample.get('timestamps')
        if type(timestamps) is not dict:
            return
        now = self.stamp()
        start = _copied_stamp(timestamps.get('transport_started'), now)
        self.note_event_started(sample.get('message_id'), sample.get('event_id'),
            sample.get('attempt'), {'transport_started':
                (start['utc_ms'], start['monotonic_ms']) if start else None})
        record = self._messages.get(sample.get('message_id')) if type(sample.get('message_id')) is str else None
        if record is None:
            return
        operation = next((row for row in record['event_attempts']
            if row['event_id'] == sample.get('event_id') and row['attempt'] == sample.get('attempt')), None)
        if operation is None or operation['outcome'] != 'sending':
            return
        outcome = sample.get('outcome')
        operation['outcome'] = outcome if type(outcome) is str and outcome in ('acknowledged', 'retry', 'failed', 'interrupted') else 'unknown'
        status = sample.get('http_status')
        operation['http_status'] = status if type(status) is int and 100 <= status <= 599 else None
        operation['finished'] = _copied_stamp(timestamps.get('transport_finished'), now)
        operation['ack'] = (_copied_stamp(timestamps.get('callback_ack'), now)
                            if operation['outcome'] == 'acknowledged' else None)
        record['timestamps']['events_transport_finished'] = operation['finished']
        if operation['ack'] is not None and record['timestamps']['events_ack'] is None:
            record['timestamps']['events_ack'] = operation['ack']

    def note_attachment(self, message_id, snapshot):
        """Call with the task's own diagnostic snapshot at start and finish."""
        if type(snapshot) is not dict:
            return
        read_id = snapshot.get('read_id')
        if type(read_id) is not str or _READ_ID.fullmatch(read_id) is None:
            return
        record = self._record(message_id)
        if record is None:
            return
        operation = next((row for row in record['attachment_reads'] if row['read_id'] == read_id), None)
        now = _stamp(self.stamp())
        outcome = snapshot.get('outcome')
        if type(outcome) is not str or outcome not in ('running', 'succeeded', 'failed', 'cancelled'):
            outcome = 'unknown'
        if operation is None:
            operation = {'read_id': read_id, 'started': now if outcome == 'running' else None,
                         'finished': None, 'outcome': 'running', 'stage': None, 'elapsed_ms': None,
                         'stages': [], 'queue_wait_ms': None}
            record['attachment_reads'].append(operation)
            record['attachment_reads'] = record['attachment_reads'][-MAX_OPERATIONS:]
        elif operation['outcome'] != 'running':
            return
        stage = snapshot.get('stage')
        operation.update(outcome=outcome, stage=stage if type(stage) is str and stage in _STAGES else None,
                         elapsed_ms=_number(snapshot.get('elapsed_ms')))
        for field in ('content_length_bytes', 'bytes_received', 'last_progress_gap_ms'):
            value = _number(snapshot.get(field))
            if value is not None:
                operation[field] = value
        timeout_kind = snapshot.get('timeout_kind')
        if type(timeout_kind) is str and timeout_kind in ('total', 'connect', 'read_idle', 'response_headers'):
            operation['timeout_kind'] = timeout_kind
        status = snapshot.get('response_status')
        if type(status) is int and 100 <= status <= 599:
            operation['response_status'] = status
        spans = snapshot.get('stages')
        if type(spans) is list:
            safe_spans = []
            for span in spans[-24:]:
                if type(span) is not dict or type(span.get('stage')) is not str or span['stage'] not in _STAGES:
                    continue
                safe_span = {'stage': span['stage'], 'elapsed_ms': _number(span.get('elapsed_ms'))}
                action = span.get('api_action')
                if type(action) is str and action in _API_ACTIONS:
                    safe_span['api_action'] = action
                if type(span.get('outcome')) is str and span['outcome'] in ('succeeded', 'failed', 'timeout'):
                    safe_span['outcome'] = span['outcome']
                safe_spans.append(safe_span)
            operation['stages'] = safe_spans
            queued = [span['elapsed_ms'] for span in safe_spans
                      if span['stage'] == 'queued' and span['elapsed_ms'] is not None]
            operation['queue_wait_ms'] = min(MAX_MS, sum(queued)) if queued else None
        if outcome != 'running':
            operation['finished'] = now

    def begin_reply(self, message_id):
        record = self._record(message_id)
        if record is None:
            return None
        token = object()
        record['reply_attempts'].append({'_token': token, 'state': 'preflight',
            'started': _stamp(self.stamp()), 'preflight_started': None, 'preflight_finished': None,
            'adapter_started': None, 'adapter_finished': None, 'finished': None})
        record['reply_attempts'] = record['reply_attempts'][-MAX_OPERATIONS:]
        return token

    def _reply(self, message_id, token):
        record = self._messages.get(message_id) if type(message_id) is str else None
        if record is not None:
            return next((row for row in record['reply_attempts']
                if row['_token'] is token and row['finished'] is None), None)
        return None

    def note_reply_stage(self, message_id, token, stage):
        if type(stage) is not str or stage not in ('preflight_started', 'preflight_finished', 'adapter_started', 'adapter_finished'):
            return
        operation = self._reply(message_id, token)
        if operation is not None and operation[stage] is None:
            operation[stage] = _stamp(self.stamp())
            if stage == 'adapter_started':
                operation['state'] = 'sending'

    def finish_reply(self, message_id, token, state):
        operation = self._reply(message_id, token)
        if operation is not None:
            operation['state'] = state if type(state) is str and state in ('adapter_completed', 'not_sent', 'uncertain') else 'uncertain'
            operation['finished'] = _stamp(self.stamp())

    def note_response(self, message_id, snapshot):
        if __package__:
            from .response_timing import ResponseTrace
        else:
            from response_timing import ResponseTrace
        safe = ResponseTrace.sanitize(snapshot)
        record = self._record(message_id)
        if safe is None or record is None or safe.get('message_id') != message_id:
            return
        traces = record.setdefault('attachment_responses', [])
        traces.append(safe)
        del traces[:-MAX_OPERATIONS]

    def note_response_queue(self, message_id, elapsed_ms):
        value = _number(elapsed_ms)
        if value is None:
            return
        record = self._record(message_id)
        if record is not None:
            waits = record.setdefault('attachment_response_queue_ms', [])
            waits.append(value)
            del waits[:-MAX_OPERATIONS]

    @staticmethod
    def _snapshot(record):
        # Private identity tokens can never leave this object.
        sample = {key: deepcopy(value) for key, value in record.items()
                  if key not in ('reply_attempts', 'rpc_attempts')}
        for field in ('reply_attempts', 'rpc_attempts'):
            sample[field] = [{key: deepcopy(value) for key, value in row.items()
                              if key != '_token'} for row in record[field]]
        for rpc in sample['rpc_attempts']:
            rpc['durations_ms'] = {
                'accepted_to_processing_ms': _elapsed(rpc['server_rpc_accepted'], rpc['processing_started']),
                'processing_ms': _elapsed(rpc['processing_started'], rpc['processing_finished'])}
        for event in sample['event_attempts']:
            event['elapsed_ms'] = _elapsed(event['started'], event['finished'])
        for reply in sample['reply_attempts']:
            reply['durations_ms'] = {
                'preflight': _elapsed(reply['preflight_started'], reply['preflight_finished']),
                'adapter': _elapsed(reply['adapter_started'], reply['adapter_finished']),
                'total_to_decision': _elapsed(reply['started'], reply['finished'])}
        stamps = sample['timestamps']
        sample['durations_ms'] = {name: _elapsed(stamps[start], stamps[end]) for name, start, end in (
            ('capture_to_ingest_ms', 'captured', 'ingest_started'),
            ('ingest_to_commit_ms', 'ingest_started', 'ingested'),
            ('capture_to_enqueue_ms', 'captured', 'enqueued'),
            ('enqueue_to_events_transport_ms', 'enqueued', 'events_transport_started'),
            ('capture_to_events_ack_ms', 'captured', 'events_ack'),
            ('unobserved_ack_to_first_read_ms', 'events_ack', 'first_tool_read'),
            ('capture_to_first_read_ms', 'captured', 'first_tool_read'))}
        first_reply = sample['reply_attempts'][0] if sample['reply_attempts'] else {}
        sample['durations_ms'].update(
            first_read_to_reply_ms=_elapsed(stamps['first_tool_read'], first_reply.get('started')),
            capture_to_reply_decision_ms=_elapsed(stamps['captured'], first_reply.get('finished')),
            capture_to_adapter_finished_ms=_elapsed(stamps['captured'], first_reply.get('adapter_finished')))
        return sample

    def trace(self, message_id, limit=MAX_OPERATIONS):
        """Return one existing record only; caller must enforce current access."""
        if (type(message_id) is not str or _MESSAGE_ID.fullmatch(message_id) is None or
                type(limit) is not int or not 1 <= limit <= MAX_OPERATIONS):
            raise ValueError('invalid_message_trace')
        record = self._messages.get(message_id)
        sample = self._snapshot(record) if record is not None else None
        truncated = []
        if sample is not None:
            for key, rows in sample.items():
                if type(rows) is list and len(rows) > limit:
                    sample[key] = rows[-limit:]
                    truncated.append(key)
        return {'message_id': message_id, 'state': 'observed' if sample is not None else 'unknown',
                'unknown_reason': None if sample is not None else 'not_retained_or_not_observed',
                'operation_limit': limit, 'truncated': truncated,
                'observability': {
                    'server_rpc_accepted': 'after_authentication_json_parse_and_metadata_validation',
                    'processing': 'tool_dispatch_including_internal_waits_not_client_receipt',
                    'accepted_to_processing': 'includes_attachment_response_slot_wait_not_all_queues',
                    'ack_to_first_read': 'unobserved',
                    'external_platform_internal': 'unknown',
                    'attachment_write_eof': 'local_write_completion_not_client_receipt'},
                'trace': sample}

    def status(self):
        recent = [self._snapshot(record) for record in self._messages.values()]
        summary = {}
        for key in ('capture_to_events_ack_ms', 'unobserved_ack_to_first_read_ms', 'capture_to_first_read_ms'):
            values = [sample['durations_ms'][key] for sample in recent if sample['durations_ms'][key] is not None]
            summary[key] = {'count': len(values), 'mean': round(sum(values) / len(values), 1) if values else None,
                            'max': max(values) if values else None}
        return {'limit': MAX_MESSAGES, 'operation_limit': MAX_OPERATIONS, 'retained': len(recent),
                'summary_ms': summary, 'recent': recent}
