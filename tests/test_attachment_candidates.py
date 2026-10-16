"""v0.5.2 invisible prefetch. Fake adapter/network only; no external downloads."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_attachments import (module, Attachments, AttachmentError, AttachmentTransport,
                              MID, OTHER, URL, fake_session, entry, dns_entry)

HTTP = 'http://media.example.com/signed?secret=never-log'
SPEC = {'segment_index': 7, 'kind': 'file', 'filename': 'notes.txt', 'url': '', 'file_id': 'captured-file'}


class CandidateTransport:
    def __init__(self):
        self.calls = []
        self.body = b'file body'
        self.block = None
        self.started = asyncio.Event()
        self.active = self.peak = 0
        self.closed = False

    def validate(self, source):
        if module._is_http_grant(source):
            return module._http_url(module._http_grant(source, transport=self)['url'])
        return AttachmentTransport().validate(source)

    async def download(self, source, consume, *, guard=None):
        self.validate(source)
        self.calls.append(source)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            if self.block is not None:
                await self.block.wait()
            if guard is not None:
                guard()
            for offset in range(0, len(self.body), 65536):
                consume(self.body[offset:offset+65536])
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


class CandidatePrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE messages(id TEXT PRIMARY KEY)')
        self.db.commit()
        self.addCleanup(self.db.close)
        self.transport = CandidateTransport()
        self.directory = Path(self.temp.name) / 'cache'
        self.group_enabled = True
        self.store = Attachments(self.db, self.directory, transport=self.transport,
            group_http_enabled=lambda: self.group_enabled)
        self.addAsyncCleanup(self.store.close)
        self.scopes = []
        self.addCleanup(lambda: [scope.close() for scope in self.scopes])

    def register(self, spec=None, mid=MID):
        with self.db:
            self.db.execute('INSERT INTO messages VALUES(?)', (mid,))
            result = self.store.register(mid, [dict(SPEC if spec is None else spec)])
        return result[0]['attachment_id']

    async def prefetch(self, cid='candidate-1', spec=None, guard=lambda: None, **kwargs):
        return await self.store.prefetch_candidate(cid, dict(SPEC if spec is None else spec),
                                                   AsyncMock(return_value=URL), guard=guard, **kwargs)

    def resolver(self, bot, guard=lambda: None):
        async def resolve(descriptor):
            scope = self.store.group_http_read(descriptor, conversation_type='group',
                bot=bot, self_id=100, group_id=200, sender_id=300, platform_id='qq', guard=guard)
            self.scopes.append(scope)
            return await scope.resolve()
        return resolve

    async def test_invisible_prefetch_no_rows_no_base64_and_remapped_adoption(self):
        resolver = AsyncMock(return_value=URL)
        with patch.object(module.base64, 'b64encode', side_effect=AssertionError('no encoded prefetch')):
            token = await self.store.prefetch_candidate('candidate-1', SPEC, resolver, guard=lambda: None)
        self.assertIs(type(token), module.PrefetchedCandidate)
        self.assertEqual(repr(token), '<PrefetchedCandidate>')
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM message_attachments').fetchone()[0], 0)
        self.assertEqual(set(resolver.call_args.args[0]), {'candidate_id', *SPEC.keys()})
        self.assertEqual(self.store.status()['registered'], 0)
        self.assertIsNone(self.store.status()['last_read'])
        self.assertTrue(all(module.CACHE_NAME.fullmatch(path.name) for path in self.directory.iterdir()))
        aid = self.register({**SPEC, 'segment_index': 11})
        self.assertTrue(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertFalse(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertFalse(self.store._candidates)
        self.assertEqual([p.name for p in self.directory.iterdir()], [aid + '.bin'])
        result = await self.store.read(MID, aid, AsyncMock(side_effect=AssertionError('cache hit')))
        self.assertEqual(base64.b64decode(result['content'][1]['resource']['blob']), self.transport.body)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_descriptor_and_token_owner_bound_and_forgery_fails(self):
        token = await self.prefetch()
        aid = self.register({**SPEC, 'file_id': 'other-file'})
        self.assertFalse(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertFalse(self.store.adopt_candidate(module.PrefetchedCandidate(), MID, aid, guard=lambda: None))
        self.assertFalse(self.store.adopt_candidate('../cache.bin', MID, aid, guard=lambda: None))
        other_dir = Path(self.temp.name) / 'other'
        other = Attachments(self.db, other_dir, transport=CandidateTransport())
        self.addAsyncCleanup(other.close)
        self.assertFalse(other.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')

    async def test_hash_corruption_is_invalidated_without_partial_adoption(self):
        token = await self.prefetch()
        path = next(self.directory.iterdir())
        path.write_bytes(b'bad bytes')
        aid = self.register()
        self.assertFalse(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(self.store._candidates)
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')

    async def test_ttl_cleans_idle_bytes_and_cannot_be_reset_by_adoption(self):
        token = await self.prefetch(ttl_seconds=.02)
        aid = self.register()
        await asyncio.sleep(.04)
        self.assertFalse(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertFalse(self.store._candidates)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_guard_rechecked_after_await_before_bytes_and_adoption(self):
        live = True
        def guard():
            if not live:
                raise AttachmentError('attachment_source_unavailable')
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.prefetch(guard=guard))
        await self.transport.started.wait()
        live = False
        self.transport.block.set()
        with self.assertRaisesRegex(AttachmentError, 'attachment_source_unavailable'):
            await task
        self.assertEqual(list(self.directory.iterdir()), [])
        live = True
        token = await self.prefetch(guard=guard)
        aid = self.register()
        live = False
        with self.assertRaisesRegex(AttachmentError, 'attachment_source_unavailable'):
            self.store.adopt_candidate(token, MID, aid, guard=lambda: None)
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')

    async def test_shared_capacity_eviction_invalidates_candidate_token(self):
        self.transport.body = b'abcd'
        with patch.object(module, 'MAX_FILE_BYTES', 4), patch.object(module, 'MAX_CACHE_BYTES', 8):
            first = await self.prefetch('first')
            await self.prefetch('second')
            # New normal read shares the same cap and evicts exactly one old candidate.
            aid = self.register({**SPEC, 'url': URL})
            await self.store.read(MID, aid)
            self.assertLessEqual(self.store.status()['cached_bytes'], 8)
            self.assertEqual(len(self.store._candidates), 1)
            self.assertEqual(len(self.store._candidate_cache_ids), 1)
            self.assertFalse(self.store.adopt_candidate(first, MID, aid, guard=lambda: None))
            self.assertFalse(self.store._reservations)

    async def test_shared_two_slots_and_close_drains_candidates_and_reads(self):
        self.transport.block = asyncio.Event()
        jobs = [asyncio.create_task(self.prefetch(str(i))) for i in range(3)]
        aid = self.register({**SPEC, 'url': URL})
        jobs.append(asyncio.create_task(self.store.read(MID, aid)))
        await asyncio.wait_for(self.transport.started.wait(), 1)
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(self.transport.peak, 2)
        await self.store.close()
        self.assertTrue(all(job.done() for job in jobs))
        self.assertTrue(all(job.cancelled() for job in jobs))
        self.assertFalse(self.store._reservations)
        self.assertFalse(self.store._candidates)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(self.transport.closed)

    async def test_discard_inflight_and_queued_releases_reservations(self):
        self.transport.block = asyncio.Event()
        task = asyncio.create_task(self.prefetch('live'))
        await self.transport.started.wait()
        self.store.discard_candidate('live')
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.store._candidates)
        self.assertFalse(self.store._reservations)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_metadata_count_bounded_and_duplicates_do_not_reset_ttl(self):
        with patch.object(module, 'MAX_PREFETCH_CANDIDATES', 2):
            first = await self.prefetch('first')
            expires = self.store._candidate_entry(first)['expires']
            again = await self.prefetch('first')
            self.assertIs(again, first)
            self.assertEqual(self.store._candidate_entry(first)['expires'], expires)
            await self.prefetch('second')
            await self.prefetch('third')
            self.assertIsNone(self.store._candidate_entry(first))
            self.assertEqual(len(self.store._candidates), 2)
            self.assertEqual(len(list(self.directory.iterdir())), 2)

    async def test_overflow_and_empty_candidate_never_leave_files(self):
        for body, code in [(b'abcde', 'attachment_too_large'), (b'', 'attachment_empty')]:
            self.transport.body = body
            with patch.object(module, 'MAX_FILE_BYTES', 4), self.assertRaisesRegex(AttachmentError, code):
                await self.prefetch()
            self.assertFalse(self.store._candidates)
            self.assertFalse(self.store._reservations)
            self.assertEqual(list(self.directory.iterdir()), [])

    async def test_adoption_requires_current_target_guard(self):
        token = await self.prefetch()
        aid = self.register()
        with self.assertRaisesRegex(AttachmentError, 'attachment_source_unavailable'):
            self.store.adopt_candidate(token, MID, aid, guard=None)
        def denied():
            raise AttachmentError('attachment_source_unavailable')
        with self.assertRaisesRegex(AttachmentError, 'attachment_source_unavailable'):
            self.store.adopt_candidate(token, MID, aid, guard=denied)
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')

    async def test_sql_full_during_adoption_does_not_claim_success_or_leave_orphan(self):
        token = await self.prefetch()
        aid = self.register()
        original_db = self.store.db
        class FullDB:
            @property
            def in_transaction(self):
                return original_db.in_transaction
            def execute(self, sql, values=()):
                if sql.startswith('UPDATE message_attachments SET size='):
                    raise sqlite3.OperationalError('database or disk is full')
                return original_db.execute(sql, values)
            def __enter__(self):
                return original_db.__enter__()
            def __exit__(self, *args):
                return original_db.__exit__(*args)
        self.store.db = FullDB()
        try:
            self.assertFalse(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        finally:
            self.store.db = original_db
        self.assertEqual(self.store.list(MID)[0]['state'], 'unfetched')
        self.assertFalse(self.store._candidates)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_caller_rollback_orphan_remains_accounted_and_reconciles(self):
        token = await self.prefetch()
        self.db.execute('BEGIN')
        self.db.execute('INSERT INTO messages VALUES(?)', (MID,))
        aid = self.store.register(MID, [SPEC])[0]['attachment_id']
        self.assertTrue(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.store.list(MID), [])
        self.assertEqual(self.store.status()['cached_bytes'], len(self.transport.body))
        await self.store.close()
        reopened = Attachments(self.db, self.directory, transport=CandidateTransport())
        self.addAsyncCleanup(reopened.close)
        self.assertEqual(reopened.status()['cached_bytes'], 0)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_captured_https_file_without_adapter_id_is_supported(self):
        spec = {**SPEC, 'file_id': '', 'url': URL}
        token = await self.prefetch(spec=spec)
        aid = self.register(spec)
        self.assertTrue(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))

    async def test_group_http_fresh_api_only_and_cached_read_fresh_again(self):
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value={'url': HTTP}))
        resolver = self.resolver(bot)
        token = await self.store.prefetch_candidate('http', SPEC, resolver, guard=lambda: None)
        bot.call_action.assert_awaited_once_with(action='get_group_file_url', group_id=200,
                                               file_id='captured-file', self_id=100)
        self.assertIs(type(self.transport.calls[0]), module._GroupHTTPGrant)
        aid = self.register({**SPEC, 'segment_index': 9})
        self.assertTrue(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        for scope in self.scopes:
            scope.close()
        result = await self.store.read(MID, aid, resolver, guard=lambda: None)
        metadata = json.loads(result['content'][0]['text'])
        self.assertEqual(metadata['transport'], 'http')
        self.assertEqual(metadata['integrity'], 'unverified')
        self.assertEqual(bot.call_action.await_count, 2)
        self.assertEqual(len(self.transport.calls), 1)
        self.group_enabled = False
        with self.assertRaisesRegex(AttachmentError, 'attachment_https_required'):
            await self.store.read(MID, aid, resolver)
        self.assertEqual(bot.call_action.await_count, 2)

    async def test_group_http_does_not_enable_raw_urls_or_forged_grants(self):
        for source in (HTTP, module._GroupHTTPGrant(), module._PrivateHTTPGrant()):
            with self.assertRaises(AttachmentError):
                await self.store.prefetch_candidate('raw', SPEC, AsyncMock(return_value=source), guard=lambda: None)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_group_api_url_private_ip_userinfo_redirect_port_fail_closed(self):
        for url in ('http://127.0.0.1/a', 'http://10.0.0.1/a', 'http://user:pw@example.com/a',
                    'http://example.com:8080/a', 'http://example.com/a#fragment'):
            bot = types.SimpleNamespace(call_action=AsyncMock(return_value={'url': url}))
            with self.assertRaises(AttachmentError):
                await self.store.prefetch_candidate('blocked', SPEC, self.resolver(bot), guard=lambda: None)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_group_guard_and_flag_changes_during_api_prevent_download(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def action(**_):
            entered.set()
            await release.wait()
            return {'url': HTTP}
        bot = types.SimpleNamespace(call_action=AsyncMock(side_effect=action))
        task = asyncio.create_task(self.store.prefetch_candidate('revoked', SPEC,
                                    self.resolver(bot), guard=lambda: None))
        await entered.wait()
        self.group_enabled = False
        release.set()
        with self.assertRaisesRegex(AttachmentError, 'attachment_https_required'):
            await task
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_startup_removes_unadopted_files_and_close_keeps_adopted(self):
        await self.prefetch('orphan')
        token = await self.prefetch('adopted')
        aid = self.register()
        self.assertTrue(self.store.adopt_candidate(token, MID, aid, guard=lambda: None))
        await self.store.close()
        self.assertEqual([p.name for p in self.directory.iterdir()], [aid + '.bin'])
        orphan = self.directory / ('att_' + 'f' * 32 + '.bin')
        orphan.write_bytes(b'old process candidate')
        orphan.chmod(0o600)
        reopened = Attachments(self.db, self.directory, transport=CandidateTransport())
        self.addAsyncCleanup(reopened.close)
        self.assertEqual([p.name for p in self.directory.iterdir()], [aid + '.bin'])


class GroupCandidateTransportTests(unittest.IsolatedAsyncioTestCase):
    # Real guarded transport; synthetic DNS/aiohttp responses only.
    asyncSetUp = CandidatePrefetchTests.asyncSetUp
    register = CandidatePrefetchTests.register
    async def grant(self, url=HTTP):
        self.transport = AttachmentTransport()
        self.store.transport = self.transport
        aid = self.register()
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value={'url': url}))
        scope = self.store.group_http_read(self.store.descriptor(MID, aid),
            conversation_type='group', bot=bot, self_id=100, group_id=200,
            sender_id=300, platform_id='qq', guard=lambda: None)
        self.scopes.append(scope)
        return await scope.resolve(), aid, scope

    async def test_group_transport_public_dns_pinning_no_redirect_and_size_guards(self):
        source, _, _ = await self.grant('http://media.example.com:80/a%2fb?secret=x%2FY')
        session, context = fake_session(chunks=(b'bytes',))
        with patch.object(self.transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
             patch.object(module.aiohttp, 'TCPConnector') as connector, \
             patch.object(module.aiohttp, 'ClientSession', return_value=context) as client:
            chunks = []
            await self.transport.download(source, chunks.append)
        self.assertEqual(chunks, [b'bytes'])
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.get.call_args.args[0].raw_path_qs, '/a%2fb?secret=x%2FY')
        resolver = connector.call_args.kwargs['resolver']
        self.assertEqual((resolver.hostname, resolver.port), ('media.example.com', 80))
        self.assertEqual((await resolver.resolve('media.example.com', 80))[0]['host'], '8.8.8.8')
        self.assertFalse(client.call_args.kwargs['trust_env'])
        with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
            await self.transport.download(source, chunks.append)

    async def test_group_http_private_dns_answer_rejected_before_request(self):
        source, _, _ = await self.grant()
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=[dns_entry('127.0.0.1', 80)])), \
             patch.object(module.aiohttp, 'ClientSession') as client:
            with self.assertRaisesRegex(AttachmentError, 'attachment_dns_blocked'):
                await self.transport.download(source, lambda _: None)
        client.assert_not_called()

    async def test_group_http_redirect_fails_without_second_request(self):
        source, _, _ = await self.grant()
        session, context = fake_session(status=302, headers={'Location': 'http://127.0.0.1/private'})
        with patch.object(self.transport, '_addresses', AsyncMock(return_value=[dict(entry(), port=80)])), \
             patch.object(module.aiohttp, 'TCPConnector'), \
             patch.object(module.aiohttp, 'ClientSession', return_value=context):
            with self.assertRaisesRegex(AttachmentError, 'attachment_redirect_blocked'):
                await self.transport.download(source, lambda _: None)
        self.assertEqual(session.get.call_count, 1)

    async def test_group_http_started_transfer_outlives_admission_window(self):
        source, _, _ = await self.grant()
        module._HTTP_GRANTS[source]['expires'] = module.time.monotonic() + .01
        session, context = fake_session()
        async def progressing_download(url, consume, *, guard=None):
            await asyncio.sleep(.025)
            module._http_grant(url, transport=self.transport)
            consume(b'continued progress')
        with patch.object(self.transport, '_download', progressing_download):
            chunks = []
            await self.transport.download(source, chunks.append)
        self.assertEqual(chunks, [b'continued progress'])

    async def test_group_http_descriptor_owner_transport_and_mode_rechecked(self):
        source, aid, scope = await self.grant()
        with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
            module._http_grant(source, owner=object())
        with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
            module._http_grant(source, transport=object())
        with self.assertRaisesRegex(AttachmentError, 'attachment_http_grant_invalid'):
            module._http_grant(source, descriptor={**scope.descriptor, 'file_id': 'different'})
        for invalid in (False, 1, 'true', None):
            self.group_enabled = invalid
            with self.assertRaisesRegex(AttachmentError, 'attachment_https_required'):
                module._http_grant(source)
        self.group_enabled = True
        with self.db:
            self.db.execute('UPDATE message_attachments SET source_file_id=? WHERE id=?', ('other-file', aid))
        with self.assertRaisesRegex(AttachmentError, 'attachment_source_unavailable'):
            module._http_grant(source)


if __name__ == '__main__':
    unittest.main()
