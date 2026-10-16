"""Exact-target diagnostics and RPC timing; loopback HTTP and fake QQ only."""
import asyncio
import json
import unittest

import test_media_request_limits as fixtures
import test_message_timing as timing_fixtures


class MessageTraceTimingTests(unittest.TestCase):
    def setUp(self):
        self.timing = timing_fixtures.MessageTiming()
        self.now = (1000, 1000)
        self.timing.stamp = lambda: self.now
        self.mid = timing_fixtures.mid()
        self.timing.note_ingest(self.mid)

    def test_rpc_attempts_are_bounded_independent_and_sanitized(self):
        self.assertIsNone(self.timing.begin_rpc(self.mid, 'https://secret.example/shell'))
        self.assertIsNone(self.timing.begin_rpc(timing_fixtures.mid(99), 'qq_read_message'))
        self.assertEqual(self.timing.status()['retained'], 1)
        for number in range(12):
            token = self.timing.begin_rpc(self.mid, 'qq_read_message')
            self.now = (1000, self.now[1] + 3)
            self.timing.start_rpc(self.mid, token)
            self.now = (900, self.now[1] + 7)
            self.timing.finish_rpc(self.mid, token, 'rpc_error' if number % 2 == 0 else 'completed')
        rows = self.timing.trace(self.mid)['trace']['rpc_attempts']
        self.assertEqual(len(rows), 8)
        self.assertEqual([row['outcome'] for row in rows], ['rpc_error', 'completed'] * 4)
        self.assertTrue(all(row['durations_ms'] == {
            'accepted_to_processing_ms': 3, 'processing_ms': 7} for row in rows))
        self.assertNotIn('_token', json.dumps(rows))
        self.assertNotIn('secret', json.dumps(rows))
        before = self.timing.trace(self.mid)
        self.timing.finish_rpc(self.mid, token, 'failed')
        self.timing.start_rpc(self.mid, token)
        self.assertEqual(self.timing.trace(self.mid), before)
        rows[-1]['tool'] = 'mutated'
        self.assertEqual(self.timing.trace(self.mid)['trace']['rpc_attempts'][-1]['tool'], 'qq_read_message')

    def test_limit_unknown_eviction_and_cancelled_before_processing(self):
        for limit in (True, False, 0, 9, 1.0, None, '1'):
            with self.assertRaises(ValueError):
                self.timing.trace(self.mid, limit)
        for number in range(4):
            token = self.timing.begin_rpc(self.mid, 'qq_reply')
            self.timing.finish_rpc(self.mid, token, 'cancelled')
            reply = self.timing.begin_reply(self.mid)
            self.timing.finish_reply(self.mid, reply, 'not_sent')
        value = self.timing.trace(self.mid, 1)
        self.assertEqual(set(value['truncated']), {'rpc_attempts', 'reply_attempts'})
        self.assertEqual(len(value['trace']['rpc_attempts']), 1)
        row = value['trace']['rpc_attempts'][0]
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertIsNone(row['processing_started'])
        self.assertIsNone(row['processing_finished'])
        self.assertIsNotNone(row['finished'])
        for number in range(2, 34):
            self.timing.note_read(timing_fixtures.mid(number))
        before = self.timing.status()
        value = self.timing.trace(self.mid)
        self.assertEqual(value['state'], 'unknown')
        self.assertIsNone(value['trace'])
        self.assertEqual(self.timing.status(), before)


class RemoteMessageTraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.MediaRequestLimitTests.asyncSetUp(self)
        self.plugin.config['remote_diagnostics_enabled'] = True
        self.timing = self.plugin.host.store.message_timing

    asyncTearDown = fixtures.MediaRequestLimitTests.asyncTearDown
    post = fixtures.MediaRequestLimitTests.post

    async def rpc(self, name, args, headers=None):
        return await self.post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': name, 'arguments': args}}, headers=headers)

    async def diagnostic(self, value=None, *, raw=None, trigger=None):
        text = 'DOT_DIAG_V1\n' + (json.dumps(value) if raw is None else raw)
        return await self.rpc('qq_reply', {'message_id': trigger or self.mid,
            'reply_key': 'diagnostic-private-key', 'text': text})

    def ingest_group(self):
        self.plugin.config.update(access_rules_version=1, private_sender_ids=['200', '456'],
                                  group_rules=[{'group_id': '345', 'mode': 'all'}])
        return self.plugin.host.store.ingest({'source_id': 'SECRET_SOURCE', 'umo': 'SECRET_ROUTE',
            'text': 'SECRET_TEXT https://private.example/?token=SECRET_TOKEN',
            'conversation_type': 'group', 'sender_id': '456', 'group_id': '345'})[0]

    async def test_exact_group_target_and_alias_are_read_only_redacted(self):
        target = self.ingest_group()
        await self.rpc('qq_read_message', {'message_id': target})
        before = self.timing.status()
        for command in ('message_trace', 'latency'):
            _, result = await self.diagnostic({'command': command, 'message_id': target, 'limit': 1})
            self.assertFalse(result['result']['isError'], result)
            value = result['result']['structuredContent']
            self.assertEqual(value['message_id'], target)
            self.assertEqual(value['state'], 'observed')
            self.assertEqual(value['trace']['message_id'], target)
            self.assertEqual(value['operation_limit'], 1)
            self.assertEqual(value['observability']['ack_to_first_read'], 'unobserved')
            self.assertEqual(value['observability']['external_platform_internal'], 'unknown')
            encoded = json.dumps(value)
            for secret in ('SECRET_', 'private.example', 'sender_id', 'group_id', self.mid,
                           'diagnostic-private-key', 'reply_key', 'arguments', '_token'):
                self.assertNotIn(secret, encoded)
        self.assertEqual(self.timing.status(), before)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.plugin.host.store.status()['replies'], {})

    async def test_strict_duplicates_extra_types_unknown_and_non_objects(self):
        good = {'command': 'message_trace', 'message_id': self.mid}
        commands = [[], None, {'command': 'shell'}, {'command': 'message_trace'},
                    {**good, 'path': '/tmp/secret'}, {**good, 'message_id': '12345'},
                    {**good, 'message_id': self.mid + '\n'}, {**good, 'command': []},
                    {'command': 'status', 'limit': 1}, {'command': 'version', 'message_id': self.mid}]
        commands += [{**good, 'limit': value} for value in (True, False, 0, 9, 1.0, None, '1', [])]
        before = self.timing.status()
        for value in commands:
            _, result = await self.diagnostic(value)
            self.assertTrue(result['result']['isError'], result)
        for raw in ('{"command":"message_trace","command":"message_trace","message_id":"%s"}' % self.mid,
                    '{"command":"message_trace","message_id":"%s","message_id":"%s"}' % (self.mid, self.mid),
                    '{"command":"message_trace","message_id":"%s","limit":1,"limit":1}' % self.mid,
                    '{"command":"message_trace","message_id":"%s","limit":NaN}' % self.mid):
            _, result = await self.diagnostic(raw=raw)
            self.assertTrue(result['result']['isError'], result)
        self.assertEqual(self.timing.status(), before)
        self.assertEqual(self.sent, [])

    async def test_default_off_owner_private_trigger_and_current_target_access(self):
        target = self.ingest_group()
        command = {'command': 'message_trace', 'message_id': target}
        for enabled in (False, 'true', 1):
            self.plugin.config['remote_diagnostics_enabled'] = enabled
            _, result = await self.diagnostic(command)
            self.assertTrue(result['result']['isError'])
        self.plugin.config['remote_diagnostics_enabled'] = True
        private = self.plugin.host.store.ingest({'source_id': 'private-456', 'umo': 'other-route',
            'text': 'other-secret', 'sender_id': '456'})[0]
        for trigger in (target, private):
            _, result = await self.diagnostic(command, trigger=trigger)
            self.assertEqual(json.loads(result['result']['content'][0]['text'])['error'],
                             'remote_diagnostics_forbidden')
        self.plugin.config['group_rules'] = []
        _, result = await self.diagnostic(command)
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('trace', result['result'])
        # A retained orphan must not be exposed after its source was removed.
        ghost = timing_fixtures.mid(999)
        self.timing.note_read(ghost)
        _, result = await self.diagnostic({'command': 'latency', 'message_id': ghost})
        self.assertTrue(result['result']['isError'])
        self.assertEqual(json.loads(result['result']['content'][0]['text'])['error'], 'message_not_found')
        self.assertEqual(self.sent, [])

    async def test_authorized_missing_observation_is_explicitly_unknown_and_not_created(self):
        for number in range(32):
            self.timing.note_read(timing_fixtures.mid(number + 1000))
        before = self.timing.status()
        _, result = await self.diagnostic({'command': 'message_trace', 'message_id': self.mid})
        self.assertFalse(result['result']['isError'])
        value = result['result']['structuredContent']
        self.assertEqual(value['state'], 'unknown')
        self.assertEqual(value['unknown_reason'], 'not_retained_or_not_observed')
        self.assertIsNone(value['trace'])
        self.assertEqual(self.timing.status(), before)

    async def test_bearer_required_and_missing_target_guard_fail_closed(self):
        before = self.timing.status()
        status, _ = await self.rpc('qq_read_message', {'message_id': self.mid},
                                  headers={**self.headers, 'Authorization': 'Bearer wrong'})
        self.assertEqual(status, 401)
        self.assertEqual(self.timing.status(), before)
        del self.plugin.host.validate_read_message
        _, result = await self.diagnostic({'command': 'message_trace', 'message_id': self.mid})
        self.assertTrue(result['result']['isError'])
        self.assertEqual(json.loads(result['result']['content'][0]['text'])['error'],
                         'remote_diagnostics_forbidden')

    async def test_rpc_success_failure_and_retry_are_separate_with_honest_stamps(self):
        _, failed = await self.rpc('qq_read_message', {'message_id': self.mid, 'extra': 'SECRET_ARG'})
        self.assertIn('error', failed)
        await self.rpc('qq_read_message', {'message_id': self.mid})
        await self.rpc('qq_read_message', {'message_id': self.mid})
        _, result = await self.diagnostic({'command': 'message_trace', 'message_id': self.mid})
        rows = result['result']['structuredContent']['trace']['rpc_attempts']
        self.assertEqual([row['outcome'] for row in rows], ['rpc_error', 'completed', 'completed'])
        for row in rows:
            self.assertEqual(row['tool'], 'qq_read_message')
            self.assertLessEqual(row['server_rpc_accepted']['monotonic_ms'], row['processing_started']['monotonic_ms'])
            self.assertLessEqual(row['processing_started']['monotonic_ms'], row['processing_finished']['monotonic_ms'])
        self.assertNotIn('SECRET_ARG', json.dumps(result))
        self.assertEqual(len(rows), len(self.timing.trace(self.mid)['trace']['rpc_attempts']))

    async def test_tool_errors_internal_failures_and_candidate_codes_are_fixed(self):
        args = {'message_id': self.mid, 'attachment_id': 'att_' + 'b' * 32}
        async def unavailable(*_):
            raise self.ep.AttachmentError('attachment_unavailable')
        self.plugin.host.read_attachment = unavailable
        _, result = await self.rpc('qq_read_attachment', args)
        self.assertTrue(result['result']['isError'])
        async def unexpected(*_):
            raise RuntimeError('SECRET_STACK https://secret.example/')
        self.plugin.host.read_attachment = unexpected
        _, result = await self.rpc('qq_read_attachment', args)
        self.assertEqual(result['error']['code'], -32603)
        expected = ['tool_error', 'failed']
        rows = self.timing.trace(self.mid)['trace']['rpc_attempts']
        self.assertEqual([row['outcome'] for row in rows], expected)
        self.assertNotIn('SECRET_', json.dumps(self.timing.trace(self.mid)))
        for code in ('candidate_context_unavailable', 'candidate_request_unavailable',
                     'candidate_scope_mismatch', 'candidate_source_changed', 'SECRET_EXTRA'):
            def rejected(_):
                raise self.ep.Rejected(code)
            self.plugin.host.validate_read_message = rejected
            _, result = await self.rpc('qq_read_message', {'message_id': self.mid})
            self.assertEqual(json.loads(result['result']['content'][0]['text'])['error'],
                             'request_rejected' if code == 'SECRET_EXTRA' else code)

    async def test_recursive_status_and_non_http_dispatch_do_not_fake_rpc_arrivals(self):
        before = self.timing.status()
        _, result = await self.diagnostic({'command': 'status'})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(self.timing.status(), before)
        await self.endpoint._dispatch('tools/call', {
            'name': 'qq_read_message', 'arguments': {'message_id': self.mid}})
        self.assertEqual(self.timing.trace(self.mid)['trace']['rpc_attempts'], [])

    async def test_attachment_slot_wait_is_separate_from_dispatch_and_write_eof(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def reader(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return {'content': [{'type': 'text', 'text': 'fake data'}], 'isError': False}
        self.plugin.host.read_attachment = reader
        args = {'message_id': self.mid, 'attachment_id': 'att_' + 'a' * 32}
        first = asyncio.create_task(self.rpc('qq_read_attachment', args))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.rpc('qq_read_attachment', args))
        try:
            for _ in range(100):
                rows = self.timing.trace(self.mid)['trace']['rpc_attempts']
                if len(rows) == 2:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(len(rows), 2)
            self.assertIsNotNone(rows[0]['processing_started'])
            self.assertIsNone(rows[1]['processing_started'])
            await asyncio.sleep(0.015)
        finally:
            release.set()
            await asyncio.gather(first, second)
        _, result = await self.diagnostic({'command': 'message_trace', 'message_id': self.mid})
        sample = result['result']['structuredContent']['trace']
        rows = sample['rpc_attempts']
        self.assertGreaterEqual(rows[1]['durations_ms']['accepted_to_processing_ms'], 10)
        self.assertEqual([row['outcome'] for row in rows], ['completed', 'completed'])
        self.assertEqual(len(sample['attachment_responses']), 2)
        for response in sample['attachment_responses']:
            self.assertIn('http_write_eof', [span['phase'] for span in response['spans']])
        self.assertNotIn('fake data', json.dumps(sample))


if __name__ == '__main__':
    unittest.main()
