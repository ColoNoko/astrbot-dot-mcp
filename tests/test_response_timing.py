"""Offline response telemetry: fake clocks/loggers, no real requests or files."""
import importlib
import json
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
module = importlib.import_module('astrbot_dot_plugin.response_timing')
ResponseTrace = module.ResponseTrace
ResponseTimingLogger = module.ResponseTimingLogger
MID = 'msg_' + '1' * 32
RID = 'read_' + '2' * 32
SECRET = 'PRIVATE_TEXT filename=SECRET.png https://secret.invalid/PATH?token=SECRET\nAuthorization: SECRET'


class NoString:
    def __str__(self):
        raise AssertionError('Untrusted objects must not be stringified')

    def __repr__(self):
        raise AssertionError('Untrusted objects must not be formatted')


class ResponseTimingTests(unittest.TestCase):
    def test_local_write_progress_is_bounded_monotonic_and_frozen(self):
        clock = [100]
        trace = ResponseTrace('endpoint', MID, stamp=lambda: (clock[0], clock[0]), logger=lambda _: None)
        trace.note_write_progress(0)
        clock[0] = 110
        trace.note_write_progress(64)
        for value in (True, -1, 32, 33554433, NoString()):
            trace.note_write_progress(value)
        clock[0] = 120
        result = trace.finish()
        self.assertEqual(result['written_bytes'], 64)
        self.assertEqual(result['last_write_progress_gap_ms'], 10)
        clock[0] = 900
        trace.note_write_progress(100)
        self.assertEqual(trace.snapshot(), result)

    def setUp(self):
        self.now = (1000, 100)
        self.logged = []
        self.trace = ResponseTrace('endpoint', MID, logger=self.logged.append,
                                   stamp=lambda: self.now)

    def finish(self):
        self.now = (1100, 200)
        return self.trace.finish()

    def test_consecutive_spans_monotonic_intervals_and_utc_clock_jump(self):
        self.trace.mark('dispatch')
        self.now = (950, 125)
        self.trace.bind_read_id(RID)
        self.trace.mark('json_encode')
        self.now = (900, 130)
        self.trace.set_encoded_bytes(4096)
        self.trace.mark('http_prepare')
        self.now = (850, 145)
        self.trace.mark('http_write_eof')
        self.now = (800, 160)
        result = self.trace.finish()
        self.assertEqual(result['message_id'], MID)
        self.assertEqual(result['read_id'], RID)
        self.assertRegex(result['response_id'], r'^response_[a-f0-9]{32}$')
        self.assertEqual(result['started'], {'utc_ms': 1000, 'monotonic_ms': 100})
        self.assertEqual(result['finished'], {'utc_ms': 800, 'monotonic_ms': 160})
        self.assertEqual(result['elapsed_ms'], 60)
        self.assertEqual(result['encoded_bytes'], 4096)
        self.assertEqual([span['phase'] for span in result['spans']],
                         ['dispatch', 'json_encode', 'http_prepare', 'http_write_eof', 'complete'])
        self.assertEqual([span['elapsed_ms'] for span in result['spans']], [25, 5, 15, 15, 0])
        self.assertEqual(self.logged, [result])
        self.assertNotIn('remote_received', result)

    def test_duplicate_finish_and_late_mutations_are_ignored(self):
        self.trace.mark('dispatch')
        self.trace.bind_read_id(RID)
        self.trace.set_encoded_bytes(42)
        self.trace.set_download({'bytes_received': 7})
        first = self.finish()
        self.now = (2000, 500)
        self.trace.mark('http_prepare')
        self.trace.bind_read_id('read_' + '3' * 32)
        self.trace.set_encoded_bytes(77)
        self.trace.set_download({'bytes_received': 99})
        self.assertEqual(self.trace.finish('failed', 'write_failed'), first)
        self.assertEqual(self.trace.snapshot(), first)
        self.assertEqual(len(self.logged), 1)
        first['spans'][0]['started']['utc_ms'] = 0
        first['download']['bytes_received'] = 0
        self.logged[0]['started']['utc_ms'] = 0
        self.assertEqual(self.trace.snapshot()['started']['utc_ms'], 1000)
        self.assertEqual(self.trace.snapshot()['spans'][0]['started']['utc_ms'], 1000)
        self.assertEqual(self.trace.snapshot()['download']['bytes_received'], 7)

    def test_failed_and_cancelled_outcomes_keep_safe_error_codes(self):
        for outcome, error, expected in (('failed', 'timeout', 'timeout'),
                ('failed', 'connection_failed', 'connection_failed'),
                ('failed', 'serialization_failed', 'serialization_failed'),
                ('failed', 'write_failed', 'write_failed'),
                ('failed', 'dispatch_failed', 'dispatch_failed'),
                ('cancelled', None, 'cancelled'), ('cancelled', SECRET, 'cancelled'),
                ('succeeded', 'write_failed', None), ('failed', SECRET, None)):
            with self.subTest(outcome=outcome, expected=expected):
                trace = ResponseTrace('main', MID, logger=self.logged.append)
                trace.mark('main_scope')
                result = trace.finish(outcome, error)
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['error'], expected)
                self.assertNotIn(SECRET, json.dumps(result))

    def test_raw_strings_objects_wrong_types_and_unknown_fields_cannot_escape(self):
        for value in (SECRET, NoString(), [], {}, True, None):
            self.trace.mark(value)
            self.trace.bind_read_id(value)
            self.trace.set_encoded_bytes(value)
            self.trace.set_download(value)
        self.trace.set_download({'read_id': SECRET, 'content_length_bytes': SECRET,
            'bytes_received': True, 'last_progress_gap_ms': float('nan'),
            'timeout_kind': SECRET, 'elapsed_ms': NoString(), 'url': SECRET,
            'headers': {'Authorization': SECRET}, 'filename': SECRET, 'text': SECRET})
        result = self.trace.finish(SECRET, SECRET)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIsNone(result['error'])
        self.assertIsNone(result['read_id'])
        self.assertIsNone(result['encoded_bytes'])
        self.assertNotIn('download', result)
        self.assertEqual([span['phase'] for span in result['spans']], ['complete'])
        self.assertNotIn('SECRET', json.dumps(result, allow_nan=False))

    def test_ids_are_opaque_exact_format_and_one_read_cannot_rebind(self):
        invalid = (SECRET, 'msg_' + '1' * 31, MID + '\n', MID.upper(), [], {}, True, NoString())
        for value in invalid:
            trace = ResponseTrace('main', value, read_id=value, logger=self.logged.append)
            self.assertIsNone(trace.snapshot()['message_id'])
            self.assertIsNone(trace.snapshot()['read_id'])
        self.trace.bind_read_id(RID + '\n')
        self.assertIsNone(self.trace.snapshot()['read_id'])
        self.trace.bind_read_id(RID)
        self.trace.bind_read_id('read_' + '3' * 32)
        self.assertEqual(self.trace.snapshot()['read_id'], RID)
        identifiers = {ResponseTrace('main', MID).response_id for _ in range(100)}
        self.assertEqual(len(identifiers), 100)
        for scope in (SECRET, None, [], {}, True, NoString()):
            with self.assertRaisesRegex(ValueError, '^invalid_response_scope$'):
                ResponseTrace(scope, MID)

    def test_spans_and_encoded_bytes_have_strict_bounds(self):
        for number in range(100):
            self.now = (1000 + number, 100 + number)
            self.trace.mark('dispatch' if number % 2 else 'json_encode')
            self.assertLessEqual(len(self.trace.snapshot()['spans']), module.MAX_SPANS)
        before = self.trace.snapshot()['spans']
        self.trace.mark('dispatch')  # The active phase is already dispatch.
        self.assertEqual(self.trace.snapshot()['spans'], before)
        for value in (-1, True, 1.0, float('inf'), module.MAX_ENCODED_BYTES + 1, 2**90):
            self.trace.set_encoded_bytes(value)
            self.assertIsNone(self.trace.snapshot()['encoded_bytes'])
        self.trace.set_encoded_bytes(module.MAX_ENCODED_BYTES)
        self.assertEqual(self.trace.snapshot()['encoded_bytes'], module.MAX_ENCODED_BYTES)
        result = self.finish()
        self.assertLessEqual(len(result['spans']), module.MAX_SPANS)
        self.assertEqual(result['spans'][-1]['phase'], 'complete')

    def test_download_summary_copies_only_bounded_numeric_fields_and_enum(self):
        diagnostic = {'read_id': RID, 'content_length_bytes': module.MAX_MS,
            'bytes_received': 1024, 'last_progress_gap_ms': 2000, 'elapsed_ms': 2200,
            'timeout_kind': 'read_idle', 'url': SECRET, 'filename': SECRET,
            'exception': NoString(), 'stages': [SECRET]}
        self.trace.set_download(diagnostic)
        diagnostic['bytes_received'] = 0
        result = self.finish()
        self.assertEqual(result['read_id'], RID)
        self.assertEqual(result['download'], {'content_length_bytes': module.MAX_MS,
            'bytes_received': 1024, 'last_progress_gap_ms': 2000, 'elapsed_ms': 2200,
            'timeout_kind': 'read_idle'})
        self.assertNotIn('SECRET', json.dumps(result))
        for kind in ('total', 'connect', 'read_idle', 'unknown'):
            safe = ResponseTrace.sanitize({**result, 'download': {'timeout_kind': kind,
                'content_length_bytes': 2**63 - 1, 'bytes_received': -1,
                'last_progress_gap_ms': 1.2, 'elapsed_ms': True}})
            self.assertEqual(safe['download'], {'timeout_kind': kind})

    def test_static_sanitizer_revalidates_every_nested_field(self):
        sample = self.finish()
        sample.update(message_id=SECRET, read_id=SECRET, error=SECRET,
                      outcome='failed', elapsed_ms=SECRET, encoded_bytes=2**63,
                      filename=SECRET, headers=SECRET)
        sample['started'] = {'utc_ms': True, 'monotonic_ms': 100, 'secret': SECRET}
        sample['finished'] = {'utc_ms': 1000, 'monotonic_ms': float('inf')}
        sample['spans'] = [{'phase': 'http_write_eof', 'started': {'utc_ms': 1, 'monotonic_ms': 2},
            'finished': {'utc_ms': 2, 'monotonic_ms': 5}, 'elapsed_ms': SECRET,
            'exception': SECRET}, {'phase': SECRET, 'elapsed_ms': 0}, NoString()]
        sample['download'] = {'bytes_received': 4, 'timeout_kind': SECRET, 'headers': SECRET}
        safe = ResponseTrace.sanitize(sample)
        self.assertIsNone(safe['message_id'])
        self.assertIsNone(safe['read_id'])
        self.assertIsNone(safe['error'])
        self.assertIsNone(safe['started'])
        self.assertIsNone(safe['finished'])
        self.assertIsNone(safe['elapsed_ms'])
        self.assertIsNone(safe['encoded_bytes'])
        self.assertEqual(len(safe['spans']), 1)
        self.assertEqual(safe['spans'][0]['elapsed_ms'], 3)
        self.assertEqual(safe['download'], {'bytes_received': 4})
        self.assertNotIn('SECRET', json.dumps(safe, allow_nan=False))

    def test_sanitizer_rejects_invalid_required_values_without_echo(self):
        sample = self.finish()
        for value in (None, [], (), NoString(), SECRET):
            self.assertIsNone(ResponseTrace.sanitize(value))
        for key in ('response_id', 'scope', 'outcome'):
            for value in (SECRET, NoString(), [], True, None):
                self.assertIsNone(ResponseTrace.sanitize({**sample, key: value}))
        safe = ResponseTrace.sanitize({**sample, 'spans': sample['spans'] * 100})
        self.assertEqual(len(safe['spans']), module.MAX_SPANS)

    def test_time_bounds_nonfinite_wrong_types_and_clock_reversal(self):
        invalid = (-1, True, 1.0, float('nan'), float('inf'), module.MAX_STAMP_MS + 1)
        for value in invalid:
            trace = ResponseTrace('main', MID, logger=self.logged.append, stamp=lambda: (value, 10))
            result = trace.finish()
            self.assertIsNone(result['started'])
            self.assertIsNone(result['finished'])
            self.assertIsNone(result['elapsed_ms'])
            json.dumps(result, allow_nan=False)
        self.trace.mark('dispatch')
        self.now = (1100, 50)
        result = self.trace.finish()
        self.assertIsNone(result['elapsed_ms'])
        self.assertIsNone(result['spans'][0]['elapsed_ms'])
        trace = ResponseTrace('main', MID, logger=self.logged.append, stamp=lambda: (0, 0))
        trace._clock = lambda: (module.MAX_STAMP_MS, module.MAX_STAMP_MS)
        self.assertEqual(trace.finish()['elapsed_ms'], module.MAX_MS)

    def test_logger_callback_failure_does_not_change_outcome_or_retry(self):
        called = []
        def fail(snapshot):
            called.append(snapshot)
            raise RuntimeError(SECRET)
        trace = ResponseTrace('main', MID, logger=fail)
        self.assertEqual(trace.finish()['outcome'], 'succeeded')
        trace.finish()
        self.assertEqual(len(called), 1)


class ResponseLoggerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.lines = []
        self.helper = ResponseTimingLogger(types.SimpleNamespace(info=self.lines.append),
                                            clock=lambda: self.now)

    def trace(self, scope='main'):
        return ResponseTrace(scope, MID, logger=self.helper)

    def test_one_json_line_per_completion_uses_only_safe_fields(self):
        trace = self.trace()
        trace.set_download({'read_id': RID, 'bytes_received': 0, 'url': SECRET})
        trace.finish('failed', 'timeout')
        trace.finish('failed', 'write_failed')
        self.assertEqual(len(self.lines), 1)
        prefix, payload = self.lines[0].split(' ', 1)
        self.assertEqual(prefix, 'dot_private_attachment_response')
        safe = json.loads(payload)
        self.assertEqual(safe['error'], 'timeout')
        self.assertEqual(safe['download'], {'bytes_received': 0})
        self.assertNotIn('SECRET', self.lines[0])
        self.assertEqual(len(self.lines[0].splitlines()), 1)

    def test_shared_rolling_rate_limit_sixty_per_minute_and_bounded_storage(self):
        for number in range(70):
            self.trace('main' if number % 2 else 'endpoint').finish()
        self.assertEqual(len(self.lines), 60)
        self.assertEqual(len(self.helper._recent), 60)
        self.now += 59.999
        self.trace().finish()
        self.assertEqual(len(self.lines), 60)
        self.now += .001
        self.trace().finish()
        self.assertEqual(len(self.lines), 61)
        self.assertEqual(len(self.helper._recent), 1)

    def test_running_or_malformed_snapshots_do_not_log_or_consume_budget(self):
        self.assertFalse(self.helper(self.trace().snapshot()))
        self.assertFalse(self.helper({'outcome': 'succeeded', 'text': SECRET}))
        self.assertFalse(self.helper(NoString()))
        self.assertEqual(self.lines, [])
        self.assertEqual(len(self.helper._recent), 0)

    def test_logger_resanitizes_callback_input(self):
        safe = ResponseTrace('main', MID, logger=lambda sample: None).finish()
        safe.update(filename=SECRET, message_id=SECRET, read_id=SECRET)
        self.assertTrue(self.helper(safe))
        self.assertNotIn('SECRET', self.lines[0])

    def test_absent_broken_native_logger_and_bad_clock_cannot_break_response(self):
        def fail(*args):
            raise RuntimeError(SECRET)
        for native in (None, types.SimpleNamespace(error=fail), types.SimpleNamespace(info=fail)):
            helper = ResponseTimingLogger(native)
            trace = ResponseTrace('main', MID, logger=helper)
            self.assertEqual(trace.finish()['outcome'], 'succeeded')
        for value in (True, -1, float('nan'), float('inf'), SECRET, NoString()):
            self.now = value
            self.trace().finish()
        self.assertEqual(self.lines, [])
        self.now = 1000.0
        self.trace().finish()
        self.now = 999.0
        self.trace().finish()
        self.assertEqual(len(self.lines), 1)


if __name__ == '__main__':
    unittest.main()
