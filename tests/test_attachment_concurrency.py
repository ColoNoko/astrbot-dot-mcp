"""Bounded attachment singleflight; synthetic APIs, bytes and local files only."""
import asyncio
import base64
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_attachments as fixtures

module = fixtures.module
AttachmentError = module.AttachmentError


class GatedTransport:
    """One deterministic gate per URL; never opens a socket or resolves DNS."""
    def __init__(self):
        self.calls = []
        self.started = asyncio.Queue()
        self.gates = {}
        self.active = self.peak = 0
        self.body = b'attachment'
        self.prefix = b''
        self.closed = False
        self.on_chunk = None
        self.cancelled = asyncio.Event()
        self.cleanup_gate = None
        self.cleanup_check = None

    def validate(self, source):
        if type(source) is module._PrivateHTTPGrant:
            return module._http_url(module._http_grant(source, transport=self)['url'])
        return module.AttachmentTransport().validate(source)

    async def download(self, source, consume, *, guard=None):
        url = (module._http_grant(source, transport=self)['url']
               if type(source) is module._PrivateHTTPGrant else source)
        gate = self.gates.setdefault(url, asyncio.Event())
        self.calls.append(url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.prefix:
                consume(self.prefix)
            self.started.put_nowait(url)
            await gate.wait()
            for offset in range(0, len(self.body), 65536):
                consume(self.body[offset:offset + 65536])
                if self.on_chunk:
                    self.on_chunk()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.cleanup_gate is not None:
                await self.cleanup_gate.wait()
                self.cleanup_check()
            raise
        finally:
            self.active -= 1

    async def close(self):
        if self.active:
            raise AssertionError('transport closed before readers drained')
        self.closed = True

    def release(self, url):
        self.gates.setdefault(url, asyncio.Event()).set()


class AttachmentConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AttachmentStoreTests.asyncSetUp

    def register_many(self, count, *, http=False):
        specs = [{'kind': 'file', 'segment_index': index, 'filename': 'file.txt',
                  'url': ('http' if http else 'https') + f'://media.example.com/{index}',
                  'file_id': f'captured-{index}'} for index in range(count)]
        with self.db:
            self.db.execute('BEGIN')
            values = self.store.register(fixtures.MID, specs)
        return [(value['attachment_id'], spec['url']) for value, spec in zip(values, specs)]

    def gated(self):
        self.transport = GatedTransport()
        self.store.transport = self.transport
        return self.transport

    async def next_started(self):
        return await asyncio.wait_for(self.transport.started.get(), 1)

    def start(self, aid, resolver=None, guard=None):
        return asyncio.create_task(self.store.read(fixtures.MID, aid, resolver, guard=guard))

    def assert_released(self):
        self.assertEqual(self.store._slots._value, 2)
        self.assertFalse(self.store._protected_ids)
        self.assertFalse(self.store._reservations)
        self.assertFalse(self.store._readers)
        self.assertFalse(self.db.in_transaction)
        self.assertFalse(list(self.directory.glob('part_*.tmp')))

    def private_resolver(self, aid, url, *, api=None, guard=lambda: None):
        bot = types.SimpleNamespace(call_action=api or AsyncMock(return_value={'url': url}))
        scope = self.store.private_http_read(self.store.descriptor(fixtures.MID, aid),
            conversation_type='private', bot=bot, self_id='100', guard=guard)
        self.addCleanup(scope.close)
        async def resolve(_descriptor):
            return await scope.resolve()
        return resolve, bot

    async def test_two_distinct_downloads_same_id_singleflight_and_third_queues(self):
        self.gated()
        (a, ua), (b, ub), (c, uc) = self.register_many(3)
        first = self.start(a)
        self.assertEqual(await self.next_started(), ua)
        follower = self.start(a)
        second = self.start(b)
        self.assertEqual(await self.next_started(), ub)
        third = self.start(c)
        await asyncio.sleep(.015)
        self.assertEqual(self.transport.calls, [ua, ub])
        self.assertEqual(self.transport.peak, 2)
        self.assertFalse(self.store._lock.locked())
        self.assertEqual(len(self.store._reservations), 2)
        for url in (ua, ub, uc):
            self.transport.release(url)
        results = await asyncio.gather(first, follower, second, third)
        self.assertTrue(all(not result['isError'] for result in results))
        self.assertEqual(self.transport.calls.count(ua), 1)
        self.assertEqual(self.transport.peak, 2)
        self.assert_released()

    async def test_completed_leader_stays_pinned_until_queued_followers_finish(self):
        self.gated()
        self.transport.body = b'x' * 80
        (a, ua), (b, ub), (c, uc) = self.register_many(3)
        with patch.object(module, 'MAX_FILE_BYTES', 100), patch.object(module, 'MAX_CACHE_BYTES', 250):
            leader, other = self.start(a), self.start(b)
            await self.next_started()
            await self.next_started()
            # Queue distinct work for a slot before a same-ID follower. Once
            # the leader finishes, C must not evict A to fit B's reservation.
            distinct = self.start(c)
            follower = self.start(a)
            await asyncio.sleep(0)
            self.transport.release(ua)
            await leader
            with self.assertRaisesRegex(AttachmentError, '^attachment_cache_full$'):
                await distinct
            self.assertFalse((await follower)['isError'])
            self.transport.release(ub)
            await other
        self.assertEqual(self.transport.calls, [ua, ub])
        self.assert_released()

    async def test_real_cache_ceiling_counts_both_twenty_mb_reservations(self):
        self.gated()
        values = self.register_many(7)
        self.assertEqual(module.MAX_CACHE_BYTES, 100 * 1024 * 1024)
        for aid, _url in values[:5]:
            path = self.directory / (aid + '.bin')
            with path.open('wb') as handle:
                handle.truncate(module.MAX_FILE_BYTES)
            path.chmod(0o600)
            with self.db:
                self.db.execute("UPDATE message_attachments SET size=?,sha256=?,mime_type=?,accessed_at=0 WHERE id=?",
                    (module.MAX_FILE_BYTES, '0' * 64, 'application/octet-stream', aid))
        self.transport.prefix = b'x' * 65536
        (a, ua), (b, ub) = values[5:]
        tasks = [self.start(a), self.start(b)]
        await self.next_started()
        await self.next_started()
        completed = sum(path.stat().st_size for path in self.directory.glob('*.bin'))
        self.assertEqual(completed, 60_000_000)
        self.assertEqual(sum(self.store._reservations.values()), 40_000_000)
        self.assertLessEqual(completed + sum(self.store._reservations.values()), module.MAX_CACHE_BYTES)
        self.assertLessEqual(self.store.status()['cached_bytes'], module.MAX_CACHE_BYTES)
        self.transport.release(ua)
        self.transport.release(ub)
        await asyncio.gather(*tasks)
        self.assert_released()

    async def test_cancelled_follower_does_not_cancel_leader(self):
        self.gated()
        (aid, url), = self.register_many(1)
        leader = self.start(aid)
        await self.next_started()
        follower = self.start(aid)
        await asyncio.sleep(0)
        follower.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower
        self.assertFalse(leader.done())
        self.assertEqual(len(self.store._reservations), 1)
        self.transport.release(url)
        self.assertFalse((await leader)['isError'])
        self.assertEqual(self.transport.calls, [url])
        self.assert_released()

    async def test_cancelled_leader_cleans_partial_and_follower_can_retry_own_read(self):
        self.gated()
        self.transport.prefix = b'partial'
        (aid, url), = self.register_many(1)
        leader = self.start(aid)
        await self.next_started()
        follower = self.start(aid)
        await asyncio.sleep(0)
        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader
        self.assertEqual(await self.next_started(), url)
        self.assertEqual(len(list(self.directory.glob('part_*.tmp'))), 1)
        self.transport.release(url)
        result = await follower
        self.assertEqual(base64.b64decode(result['content'][1]['resource']['blob']),
                         b'partialattachment')
        self.assertEqual(self.transport.calls, [url, url])
        self.assert_released()

    async def test_follower_rechecks_own_guard_before_receiving_cached_bytes(self):
        self.gated()
        (aid, url), = self.register_many(1)
        allowed = True
        def guard():
            if not allowed:
                raise AttachmentError('attachment_stopping')
        leader = self.start(aid)
        await self.next_started()
        follower = self.start(aid, guard=guard)
        await asyncio.sleep(0)
        allowed = False
        self.transport.release(url)
        await leader
        with self.assertRaisesRegex(AttachmentError, '^attachment_stopping$'):
            await follower
        self.assertEqual(self.transport.calls, [url])
        self.assert_released()

    async def test_http_followers_obtain_their_own_fresh_api_authorization(self):
        self.gated()
        self.store._private_http_enabled = lambda: True
        (aid, url), = self.register_many(1, http=True)
        one, first_bot = self.private_resolver(aid, url)
        two, second_bot = self.private_resolver(aid, url)
        leader = self.start(aid, one)
        await self.next_started()
        follower = self.start(aid, two)
        await asyncio.sleep(0)
        second_bot.call_action.assert_not_awaited()
        self.transport.release(url)
        await asyncio.gather(leader, follower)
        first_bot.call_action.assert_awaited_once()
        second_bot.call_action.assert_awaited_once()
        self.assertEqual(self.transport.calls, [url])
        self.assert_released()

    async def test_http_mode_disabled_while_queued_blocks_api_and_cached_bytes(self):
        self.gated()
        enabled = True
        self.store._private_http_enabled = lambda: enabled
        (aid, url), = self.register_many(1, http=True)
        resolver, _bot = self.private_resolver(aid, url)
        self.transport.release(url)
        await self.store.read(fixtures.MID, aid, resolver)
        waiting, bot = self.private_resolver(aid, url)
        await self.store._slots.acquire()
        await self.store._slots.acquire()
        task = self.start(aid, waiting)
        await asyncio.sleep(0)
        enabled = False
        self.store._slots.release()
        self.store._slots.release()
        with self.assertRaisesRegex(AttachmentError, '^attachment_https_required$'):
            await task
        bot.call_action.assert_not_awaited()
        self.assertEqual(self.transport.calls, [url])
        self.assert_released()

    async def test_private_api_oversize_hint_rejects_before_any_download_or_reservation(self):
        self.gated()
        self.store._private_http_enabled = lambda: True
        (aid, url), = self.register_many(1, http=True)
        for key in ('size', 'file_size'):
            resolver, _ = self.private_resolver(aid, url,
                api=AsyncMock(return_value={'url': url, key: '20000001'}))
            with self.assertRaisesRegex(AttachmentError, '^attachment_too_large$'):
                await self.store.read(fixtures.MID, aid, resolver)
        self.assertFalse(self.transport.calls)
        self.assert_released()

    async def test_network_failure_releases_both_active_reservations(self):
        self.gated()
        self.transport.prefix = b'partial'
        (a, ua), (b, ub) = self.register_many(2)
        first, second = self.start(a), self.start(b)
        await self.next_started()
        await self.next_started()
        def fail():
            raise AttachmentError('attachment_network_failed')
        self.transport.on_chunk = fail
        self.transport.release(ua)
        self.transport.release(ub)
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(value, AttachmentError) for value in results))
        self.assertFalse(list(self.directory.iterdir()))
        self.assert_released()

    async def test_reservations_count_partial_files_without_exceeding_cache_cap(self):
        self.gated()
        self.transport.body = b'x' * 60
        self.transport.prefix = b'p' * 20
        (a, ua), (b, ub), (c, uc) = self.register_many(3)
        self.transport.release(ua)
        with patch.object(module, 'MAX_FILE_BYTES', 100), patch.object(module, 'MAX_CACHE_BYTES', 250):
            await self.store.read(fixtures.MID, a)
            first = self.start(b)
            await self.next_started()  # Warm-up download.
            await self.next_started()
            self.assertTrue((self.directory / (a + '.bin')).exists())
            second = self.start(c)
            await self.next_started()
            self.assertFalse((self.directory / (a + '.bin')).exists())
            self.assertEqual(sum(self.store._reservations.values()), 200)
            def bounded():
                self.assertLessEqual(sum(path.stat().st_size for path in self.directory.iterdir()), 250)
            self.transport.on_chunk = bounded
            bounded()
            self.transport.release(ub)
            self.transport.release(uc)
            await asyncio.gather(first, second)
        self.assertEqual(sum(path.stat().st_size for path in self.directory.iterdir()), 160)
        self.assert_released()

    async def test_eviction_never_removes_cached_http_read_awaiting_fresh_api(self):
        self.gated()
        self.transport.body = b'x' * 80
        self.store._private_http_enabled = lambda: True
        (a, ua), (b, ub), (c, uc) = self.register_many(3, http=True)
        for url in (ua, ub, uc):
            self.transport.release(url)
        with patch.object(module, 'MAX_FILE_BYTES', 100), patch.object(module, 'MAX_CACHE_BYTES', 250):
            for aid, url in ((a, ua), (b, ub)):
                resolver, _ = self.private_resolver(aid, url)
                await self.store.read(fixtures.MID, aid, resolver)
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_api(**_kwargs):
                entered.set()
                await release.wait()
                return {'url': ua}
            resolver, _ = self.private_resolver(a, ua, api=AsyncMock(side_effect=slow_api))
            protected = self.start(a, resolver)
            await entered.wait()
            resolver, _ = self.private_resolver(c, uc)
            await self.store.read(fixtures.MID, c, resolver)
            self.assertTrue((self.directory / (a + '.bin')).exists())
            self.assertFalse((self.directory / (b + '.bin')).exists())
            release.set()
            await protected
        self.assertEqual(self.transport.calls.count(ua), 1)
        self.assert_released()

    async def test_capacity_exhaustion_releases_reservation_and_preserves_active_fill(self):
        self.gated()
        (a, ua), (b, ub) = self.register_many(2)
        with patch.object(module, 'MAX_FILE_BYTES', 100), patch.object(module, 'MAX_CACHE_BYTES', 150):
            first = self.start(a)
            await self.next_started()
            with self.assertRaisesRegex(AttachmentError, '^attachment_cache_full$'):
                await self.store.read(fixtures.MID, b)
            self.assertEqual(self.transport.calls, [ua])
            self.assertEqual(sum(self.store._reservations.values()), 100)
            self.transport.release(ua)
            await first
        self.assert_released()

    async def test_close_drains_active_duplicate_and_slot_waiters_before_resources_close(self):
        self.gated()
        self.transport.prefix = b'partial'
        self.transport.cleanup_gate = asyncio.Event()
        def check():
            self.db.execute('SELECT 1')
            self.assertIsNotNone(self.store._dir)
        self.transport.cleanup_check = check
        (a, _ua), (b, _ub), (c, _uc) = self.register_many(3)
        tasks = [self.start(a), self.start(b)]
        await self.next_started()
        await self.next_started()
        tasks.extend((self.start(a), self.start(c)))
        await asyncio.sleep(0)
        close = asyncio.create_task(self.store.close())
        await self.transport.cancelled.wait()
        close.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        self.assertIsNotNone(self.store._dir)
        self.assertFalse(self.transport.closed)
        self.transport.cleanup_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await close
        values = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in values))
        self.assertTrue(self.transport.closed)
        self.assertIsNone(self.store._dir)
        self.assert_released()
        await self.store.close()  # Idempotent, including after caller cancellation.
        with self.assertRaisesRegex(AttachmentError, '^attachment_stopping$'):
            await self.store.read(fixtures.MID, a)

    async def test_each_read_keeps_its_own_slot_and_singleflight_queue_timing(self):
        self.gated()
        timing = importlib.import_module('astrbot_dot_plugin.message_timing').MessageTiming()
        self.store.message_timing = timing
        (a, ua), (b, ub), (c, uc) = self.register_many(3)
        first, second = self.start(a), self.start(b)
        await self.next_started()
        await self.next_started()
        follower, third = self.start(a), self.start(c)
        await asyncio.sleep(.025)
        for url in (ua, ub, uc):
            self.transport.release(url)
        await asyncio.gather(first, second, follower, third)
        reads = timing.status()['recent'][0]['attachment_reads']
        self.assertEqual(len(reads), 4)
        self.assertEqual(len({read['read_id'] for read in reads}), 4)
        self.assertTrue(all(read['outcome'] == 'succeeded' for read in reads))
        self.assertGreaterEqual(reads[2]['queue_wait_ms'], 20)
        self.assertGreaterEqual(reads[3]['queue_wait_ms'], 20)
        self.assertEqual([span['stage'] for span in reads[2]['stages']],
                         ['queued', 'validation', 'cache_io', 'result', 'complete'])
        self.assert_released()

    async def test_exact_twenty_million_bytes_return_once_and_overflow_cleans_up(self):
        self.assertEqual(module.MAX_FILE_BYTES, 20_000_000)
        self.assertEqual(module.MAX_OUTPUT_BYTES, 32 * 1024 * 1024)
        (a, _ua), (b, _ub) = self.register_many(2)
        self.transport.body = b'x' * module.MAX_FILE_BYTES
        result = await self.store.read(fixtures.MID, a)
        blob = result['content'][1]['resource']['blob']
        self.assertEqual(len(base64.b64decode(blob)), module.MAX_FILE_BYTES)
        self.assertEqual(len(result['content']), 2)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), module.MAX_OUTPUT_BYTES)
        self.transport.body += b'x'
        with self.assertRaises(AttachmentError) as failure:
            await self.store.read(fixtures.MID, b)
        self.assertEqual(failure.exception.code, 'attachment_too_large')
        self.assertEqual(failure.exception.size_diagnostic,
                         {'max_bytes': 20_000_000, 'known_size_bytes': 20_000_001})
        self.assertFalse((self.directory / (b + '.bin')).exists())
        self.assert_released()

    async def test_cache_hit_larger_than_current_limit_rejects_before_read_or_api(self):
        (aid, _url), = self.register_many(1)
        self.transport.body = b'x' * 100
        await self.store.read(fixtures.MID, aid)
        with patch.object(module, 'MAX_FILE_BYTES', 99), \
                patch.object(self.store, '_bytes', side_effect=AssertionError('no bytes')):
            with self.assertRaisesRegex(AttachmentError, '^attachment_too_large$'):
                await self.store.read(fixtures.MID, aid, AsyncMock())
        self.assertEqual(len(self.transport.calls), 1)
        self.assert_released()


class AttachmentSizeValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_size_hints_accept_only_bounded_strict_integer_shapes(self):
        for value in (None, True, False, -1, 1.5, {}, [], '020000001', '+20000001',
                      ' 20000001', '2e8', '9' * 21, 20_000_000, '20000000'):
            with self.subTest(value=value):
                module.validate_known_size(value)
        for value in (20_000_001, '20000001', 10**40, '99999999999999999999'):
            with self.subTest(value=value), self.assertRaises(AttachmentError) as failure:
                module.validate_known_size(value)
            diagnostic = failure.exception.size_diagnostic
            self.assertEqual(diagnostic['max_bytes'], 20_000_000)
            self.assertEqual('known_size_bytes' in diagnostic, int(value) <= 2**63 - 1)

    async def test_content_length_early_reject_and_unknown_or_false_length_stream_cap(self):
        transport = module.AttachmentTransport()
        self.addAsyncCleanup(transport.close)
        for length, chunks, expected, received in (
                (101, (b'never consumed',), 'attachment_too_large', b''),
                (None, (b'a' * 100,), None, b'a' * 100),
                (1, (b'a' * 100, b'x'), 'attachment_too_large', b'a' * 100),
                (None, (b'a' * 100, b'x'), 'attachment_too_large', b'a' * 100)):
            with self.subTest(length=length, expected=expected):
                _session, context = fixtures.fake_session(length=length, chunks=chunks)
                parts = []
                with patch.object(module, 'MAX_FILE_BYTES', 100), \
                        patch.object(transport, '_addresses', AsyncMock(return_value=[fixtures.entry()])), \
                        patch.object(module.aiohttp, 'ClientSession', return_value=context):
                    if expected:
                        with self.assertRaisesRegex(AttachmentError, '^' + expected + '$'):
                            await transport.download(fixtures.URL, parts.append)
                    else:
                        await transport.download(fixtures.URL, parts.append)
                self.assertEqual(b''.join(parts), received)
