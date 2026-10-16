"""Offline Tunnel-only lifecycle tests. No executable or network is used."""
import asyncio
import base64
import copy
import hashlib
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_main import Event, load_main


class Config(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


class FakeTunnel:
    def __init__(self, calls):
        self.calls = calls
        self.running = True
        self.close_hook = None
        self.close_error = None
        self.leave_running = False
        self.close_count = 0

    def status(self):
        return {'enabled': True, 'state': 'running' if self.running else 'stopped',
                'process_running': self.running, 'healthy': self.running,
                'ready': self.running, 'connected': None,
                'roundtrip_verified': False, 'last_error_code': None}

    async def close(self):
        self.close_count += 1
        self.calls.append(('close', self))
        if self.close_hook:
            await self.close_hook()
        if self.close_error:
            raise self.close_error
        if not self.leave_running:
            self.running = False


class TunnelReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=True, platform_id='instance-1', adapter='aiocqhttp',
            self_id='100', sender_id='200', mcp_token='DUMMY_MCP_' + 'x' * 40,
            tunnel_enabled=True, tunnel_id='tunnel_' + 'a' * 32,
            tunnel_runtime_key='DUMMY_TUNNEL_' + 'y' * 40,
            callback_hosts=['callbacks.example.com'], access_rules_version=1,
            private_sender_ids=['200'], group_rules=[])
        class Endpoint:
            def __init__(self, host, *args, **kwargs):
                self.host = host
                self.start_count = self.close_count = 0
                self.events = None
                self._delivery_tasks = []
                self.kwargs = kwargs

            async def start(self):
                self.start_count += 1

            async def close(self):
                self.close_count += 1
                for task in self._delivery_tasks:
                    task.cancel()
                await asyncio.gather(*self._delivery_tasks, return_exceptions=True)
                if self.events:
                    await self.events.transport.close()

        self.module.Endpoint = Endpoint
        self.p = self.module.Main(object(), self.config)
        self.calls = []
        self.tunnels = []
        self.start_hook = None
        self.start_configs = []

        async def start_tunnel(root):
            self.assertIsNone(self.p.tunnel, 'replacement must not overwrite a supervisor')
            self.assertFalse(any(t.running for t in self.tunnels), 'Tunnel overlap')
            self.start_configs.append(copy.deepcopy(dict(self.config)))
            tunnel = FakeTunnel(self.calls)
            self.tunnels.append(tunnel)
            self.p.tunnel = tunnel
            self.p.last_tunnel_status = None
            self.calls.append(('start', tunnel))
            if self.start_hook:
                await self.start_hook(tunnel)

        self.p._start_tunnel = start_tunnel
        self.req = types.SimpleNamespace(username='local-admin',
            plugin_name='astrbot_plugin_dot_private')
        self.no_process = patch('asyncio.create_subprocess_exec',
            new=AsyncMock(side_effect=AssertionError('executables forbidden in synthetic test')))
        self.no_process.start()
        await self.p.initialize()
        self.assertTrue(self.p.ready)
        self.old = self.p.tunnel
        self.calls.clear()
        self.config.saves = 0

    async def asyncTearDown(self):
        if self.p.tunnel is not None:
            self.p.tunnel.close_hook = self.p.tunnel.close_error = None
            self.p.tunnel.leave_running = False
        await self.p.terminate()
        self.no_process.stop()
        self.tmp.cleanup()

    def payload(self, **updates):
        status = self.p.admin.status(self.req)
        payload = {'csrf_token': status['csrf_token'], 'revision': status['revision'],
                   'action': 'reconnect_tunnel'}
        payload.update(updates)
        return payload

    async def reconnect(self, payload=None, request=None):
        return await self.p.admin.mutate(request or self.req,
            payload if payload is not None else self.payload(), 'action')

    def assert_runtime_kept(self, host, endpoint):
        self.assertIs(self.p.host, host)
        self.assertIs(self.p.endpoint, endpoint)
        self.assertTrue(self.p.ready)
        self.assertFalse(host.stopping)
        self.assertEqual(endpoint.start_count, 1)
        self.assertEqual(endpoint.close_count, 0)

    async def test_success_preserves_endpoint_events_database_workers_handles_and_dedupe(self):
        host, endpoint = self.p.host, self.p.endpoint
        store, db = host.store, host.store.db
        events = importlib.import_module('plugin_mock.events').Events(
            db, 'astrbot-bound-owner', ['callbacks.example.com'])
        await events.transport.close()

        class Transport:
            def validate(self, url):
                assert url == 'https://callbacks.example.com/events'

            async def post(self, url, body, headers):
                return 200, json.dumps({'challenge': json.loads(body)['challenge']}).encode()

            async def close(self):
                pass

        events.transport = Transport()
        endpoint.events = store.events = events
        await events.subscribe({'name': 'qq.message', 'arguments': {}, 'ttlMs': None,
            'delivery': {'mode': 'webhook', 'url': 'https://callbacks.example.com/events',
                         'secret': 'whsec_' + base64.b64encode(b's' * 32).decode()}})
        gate = asyncio.Event()
        endpoint._delivery_tasks = [asyncio.create_task(gate.wait()) for _ in range(2)]
        workers = tuple(endpoint._delivery_tasks)
        event = Event()
        await self.p.on_private_message(event)
        mid = next(iter(self.p.captured))
        staging = host.media_staging
        upload = staging.begin(mid, 'pending-media-key', 'file', 'synthetic.txt',
                               4, hashlib.sha256(b'test').hexdigest())
        await host.reply(mid, 'first reply', 'synthetic-reply-key')
        self.assertEqual(len(event.sent), 1)
        captured, bots = self.p.captured, self.p.captured_bots
        subscriptions = db.execute('SELECT * FROM event_subscriptions').fetchall()
        deliveries = db.execute('SELECT * FROM event_deliveries').fetchall()
        config = copy.deepcopy(dict(self.config))
        self.p._ensure_mcp_token = lambda: self.fail('reconnect must never mint credentials')

        result = await self.reconnect()

        self.assertEqual([c[0] for c in self.calls], ['close', 'start'])
        self.assertIsNot(self.p.tunnel, self.old)
        self.assertFalse(self.old.running)
        self.assert_runtime_kept(host, endpoint)
        self.assertIs(self.p.host.store, store)
        self.assertIs(store.db, db)
        self.assertIs(endpoint.events, events)
        self.assertIs(store.events, events)
        self.assertEqual(tuple(endpoint._delivery_tasks), workers)
        self.assertTrue(all(not task.done() for task in workers))
        self.assertEqual(db.execute('SELECT * FROM event_subscriptions').fetchall(), subscriptions)
        self.assertEqual(db.execute('SELECT * FROM event_deliveries').fetchall(), deliveries)
        self.assertIs(self.p.captured, captured)
        self.assertIs(self.p.captured_bots, bots)
        self.assertIs(captured[mid], event)
        self.assertIs(host.media_staging, staging)
        self.assertEqual(staging.status()['active_uploads'], 1)
        self.assertEqual(staging._uploads[upload['upload_id']].offset, 0)
        await host.reply(mid, 'first reply', 'synthetic-reply-key')
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(dict(self.config), config)
        self.assertEqual(self.config.saves, 0)
        self.assertEqual(self.start_configs[0], self.start_configs[-1])
        self.assertNotIn(self.config['mcp_token'], json.dumps(result))
        self.assertNotIn(self.config['tunnel_runtime_key'], json.dumps(result))

    async def test_native_authorization_csrf_revision_and_payload_are_required(self):
        cases = [({'csrf_token': 'wrong'}, None, 'forbidden'),
                 ({'revision': 'stale'}, None, 'revision_conflict'),
                 ({'settings': {}}, None, 'invalid_settings'),
                 ({}, types.SimpleNamespace(username='other', plugin_name=self.req.plugin_name), 'forbidden'),
                 ({}, types.SimpleNamespace(username='local-admin', plugin_name='other'), 'forbidden')]
        for update, req, code in cases:
            with self.subTest(code=code, update=update):
                with self.assertRaisesRegex(Exception, '^' + code + '$'):
                    await self.reconnect(self.payload(**update), req)
        self.assertEqual(self.calls, [])

    async def test_native_action_handler_reuses_auth_and_returns_sanitized_status(self):
        web = types.ModuleType('astrbot.api.web')
        web.request = types.SimpleNamespace(username=self.req.username,
            plugin_name=self.req.plugin_name, content_type='application/json',
            body=AsyncMock(return_value=json.dumps(self.payload()).encode()))
        web.json_response = lambda value, headers: dict(status=200, value=value, headers=headers)
        web.error_response = lambda code, status_code, headers: dict(status=status_code, error=code)
        with patch.dict(sys.modules, {'astrbot.api.web': web}):
            result = await self.p.page_action()
            self.assertEqual(result['status'], 200)
            self.assertTrue(result['value']['runtime']['mcp_ready'])
            self.assertEqual(result['headers']['Cache-Control'], 'no-store')
            web.request.username = ''
            self.assertEqual((await self.p.page_action())['status'], 403)
        self.assertNotIn(self.config['mcp_token'], json.dumps(result))
        self.assertNotIn(self.config['tunnel_runtime_key'], json.dumps(result))

    async def test_real_tunnel_factory_reuses_only_existing_runtime_values(self):
        module = importlib.import_module('plugin_mock.tunnel_runtime')
        before = copy.deepcopy(dict(self.config))
        calls = self.calls
        captured = []
        class Supervisor(FakeTunnel):
            def __init__(self, private, binary):
                super().__init__(calls)
                self.running = False
                self.private = private

            async def start(self, settings, **kwargs):
                captured.append((settings, kwargs, Path(settings.runtime_key_file).read_text()))
                self.running = True

        self.p._start_tunnel = self.module.Main._start_tunnel.__get__(self.p)
        self.p._ensure_mcp_token = lambda: self.fail('reconnect must reuse credentials')
        with patch.object(module, 'TunnelSupervisor', Supervisor):
            await self.reconnect()
        self.assertEqual(len(captured), 1)
        settings, values, key = captured[0]
        self.assertEqual(settings.tunnel_id, before['tunnel_id'])
        self.assertEqual(key, before['tunnel_runtime_key'])
        self.assertEqual(values['mcp_token'], before['mcp_token'])
        self.assertEqual(values['endpoint_url'], 'http://127.0.0.1:8769/mcp')
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.config.saves, 0)
        self.assertEqual(Path(settings.runtime_key_file).stat().st_mode & 0o777, 0o600)

    async def test_disabled_or_not_ready_never_disconnects_or_starts(self):
        for name, value, code in [('enabled', False, 'runtime_not_ready'),
                                  ('tunnel_enabled', False, 'tunnel_disabled')]:
            old = self.config[name]
            self.config[name] = value
            with self.assertRaisesRegex(Exception, '^' + code + '$'):
                await self.reconnect()
            self.config[name] = old
        self.p.ready = False
        with self.assertRaisesRegex(Exception, '^runtime_not_ready$'):
            await self.reconnect()
        self.p.ready = True
        self.p.host.stopping = True
        with self.assertRaisesRegex(Exception, '^runtime_not_ready$'):
            await self.reconnect()
        self.p.host.stopping = False
        self.assertEqual(self.calls, [])

    async def test_pending_native_config_never_implicitly_applies(self):
        for key, value in [('listen_port', 9876), ('tunnel_id', 'tunnel_' + 'b' * 32),
                           ('mcp_token', 'new_' + 'z' * 40), ('tunnel_runtime_key', ''),
                           ('proxy_enabled', True), ('sender_id', '999'),
                           ('callback_hosts', ['changed.example.com'])]:
            before = copy.deepcopy(dict(self.config))
            self.config[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(Exception, '^runtime_config_changed$'):
                await self.reconnect()
            self.config.clear()
            self.config.update(before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config.saves, 0)

    async def test_invalid_existing_credentials_never_mint_or_disconnect(self):
        before = copy.deepcopy(dict(self.config))
        for key in ('mcp_token', 'tunnel_runtime_key'):
            for value in ('', 'short', 'x' * 31 + '\n', 'x' * 4097):
                self.config[key] = value
                # Exercise defense-in-depth for an invalid legacy runtime snapshot.
                self.p._runtime_config_revision = self.p.admin._revision()
                with self.subTest(key=key, size=len(value)), self.assertRaisesRegex(Exception, '^invalid_settings$'):
                    await self.reconnect()
                self.config.clear()
                self.config.update(before)
        self.p._runtime_config_revision = self.p.admin._revision()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config.saves, 0)

    async def test_revision_is_rechecked_after_waiting_for_lifecycle_lock(self):
        await self.p._runtime_lock.acquire()
        task = asyncio.create_task(self.reconnect())
        await asyncio.sleep(0)
        self.config['listen_port'] = 9876
        self.p._runtime_lock.release()
        with self.assertRaisesRegex(Exception, '^revision_conflict$'):
            await task
        self.assertEqual(self.calls, [])

    async def test_changes_during_stop_prevent_restart(self):
        host, endpoint = self.p.host, self.p.endpoint
        for key, value in [('enabled', False), ('tunnel_enabled', False),
                           ('listen_port', 9876), ('mcp_token', 'z' * 40)]:
            before = copy.deepcopy(dict(self.config))
            async def change():
                self.config[key] = value
            self.old.running = True
            self.p.tunnel = self.old
            self.old.close_hook = change
            with self.subTest(key=key), self.assertRaisesRegex(Exception, '^revision_conflict$'):
                await self.reconnect()
            self.assertIsNone(self.p.tunnel)
            self.config.clear()
            self.config.update(before)
        self.assert_runtime_kept(host, endpoint)
        self.assertFalse(any(action == 'start' for action, _ in self.calls))

    async def test_binding_change_during_stop_prevents_restart(self):
        original = self.p.host.store.binding
        async def change():
            self.p.host.store.binding = self.module.Binding('instance-1', 'aiocqhttp', '100', '999')
        self.old.close_hook = change
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            await self.reconnect()
        self.p.host.store.binding = original
        self.assertIsNone(self.p.tunnel)
        self.assertEqual([action for action, _ in self.calls], ['close'])

    async def test_terminate_during_stop_vetoes_restart_and_waits_for_serialized_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def block():
            entered.set()
            await release.wait()
        self.old.close_hook = block
        reconnect = asyncio.create_task(self.reconnect())
        await entered.wait()
        terminate = asyncio.create_task(self.p.terminate())
        await asyncio.sleep(0)
        self.assertFalse(terminate.done())
        release.set()
        with self.assertRaisesRegex(Exception, '^forbidden$'):
            await reconnect
        await terminate
        self.assertEqual([action for action, _ in self.calls], ['close'])
        self.assertIsNone(self.p.tunnel)
        self.assertIsNone(self.p.endpoint)
        self.assertFalse(self.p.ready)

    async def test_failed_old_close_retains_reference_and_keeps_endpoint(self):
        host, endpoint = self.p.host, self.p.endpoint
        self.old.close_error = RuntimeError('RAW_SECRET_ERROR')
        for _ in range(2):
            with self.assertRaisesRegex(Exception, '^tunnel_stop_incomplete$'):
                await self.reconnect()
            self.assertIs(self.p.tunnel, self.old)
            self.assert_runtime_kept(host, endpoint)
        self.assertEqual([action for action, _ in self.calls], ['close', 'close'])
        self.assertEqual(self.p.runtime_error, 'tunnel_stop_incomplete')

    async def test_running_after_close_is_incomplete_and_blocks_replacement(self):
        self.old.leave_running = True
        with self.assertRaisesRegex(Exception, '^tunnel_stop_incomplete$'):
            await self.reconnect()
        self.assertIs(self.p.tunnel, self.old)
        self.assertEqual([action for action, _ in self.calls], ['close'])

    async def test_cancelled_old_close_finishes_before_lock_release_and_does_not_restart(self):
        host, endpoint = self.p.host, self.p.endpoint
        entered, release = asyncio.Event(), asyncio.Event()
        async def block():
            entered.set()
            await release.wait()
        self.old.close_hook = block
        task = asyncio.create_task(self.reconnect())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.p._runtime_lock.locked())
        self.assertIs(self.p.tunnel, self.old)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.p.tunnel)
        self.assertFalse(self.old.running)
        self.assert_runtime_kept(host, endpoint)
        self.assertEqual([action for action, _ in self.calls], ['close'])

    async def test_self_cancelled_close_keeps_supervisor_reference(self):
        self.old.close_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.reconnect()
        self.assertIs(self.p.tunnel, self.old)
        self.assertEqual(self.p.runtime_error, 'tunnel_stop_incomplete')
        self.assertEqual([action for action, _ in self.calls], ['close'])

    async def test_failed_replacement_is_closed_and_only_tunnel_is_affected(self):
        host, endpoint = self.p.host, self.p.endpoint
        async def fail(tunnel):
            raise RuntimeError('RAW_PRIVATE_KEY_FROM_NETWORK')
        self.start_hook = fail
        with self.assertRaisesRegex(Exception, '^tunnel_reconnect_failed$'):
            await self.reconnect()
        self.assertEqual([action for action, _ in self.calls], ['close', 'start', 'close'])
        self.assertIsNone(self.p.tunnel)
        self.assertFalse(any(t.running for t in self.tunnels))
        self.assert_runtime_kept(host, endpoint)
        self.assertEqual(self.p.runtime_error, 'tunnel_reconnect_failed')
        self.start_hook = None
        await self.reconnect()
        self.assertIsNone(self.p.runtime_error)
        self.assert_runtime_kept(host, endpoint)

    async def test_replacement_cleanup_failure_retains_handle(self):
        async def fail(tunnel):
            tunnel.close_error = RuntimeError('PRIVATE_STOP_ERROR')
            raise RuntimeError('PRIVATE_START_ERROR')
        self.start_hook = fail
        with self.assertRaisesRegex(Exception, '^tunnel_stop_incomplete$'):
            await self.reconnect()
        replacement = self.p.tunnel
        self.assertIs(replacement, self.tunnels[-1])
        self.assertIsNot(replacement, self.old)
        with self.assertRaisesRegex(Exception, '^tunnel_stop_incomplete$'):
            await self.reconnect()
        self.assertEqual(len(self.tunnels), 2)

    async def test_cancelled_replacement_is_closed_without_losing_endpoint(self):
        host, endpoint = self.p.host, self.p.endpoint
        entered = asyncio.Event()
        async def block(tunnel):
            entered.set()
            await asyncio.Event().wait()
        self.start_hook = block
        task = asyncio.create_task(self.reconnect())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.p.tunnel)
        self.assertFalse(any(t.running for t in self.tunnels))
        self.assert_runtime_kept(host, endpoint)
        self.assertEqual([action for action, _ in self.calls], ['close', 'start', 'close'])

    async def test_config_change_during_start_closes_replacement(self):
        host, endpoint = self.p.host, self.p.endpoint
        async def change(tunnel):
            self.config['listen_port'] = 9876
        self.start_hook = change
        with self.assertRaisesRegex(Exception, '^revision_conflict$'):
            await self.reconnect()
        self.assertIsNone(self.p.tunnel)
        self.assertFalse(any(t.running for t in self.tunnels))
        self.assert_runtime_kept(host, endpoint)

    async def test_terminate_during_start_closes_replacement_then_runtime(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(tunnel):
            entered.set()
            await release.wait()
        self.start_hook = block
        reconnect = asyncio.create_task(self.reconnect())
        await entered.wait()
        terminate = asyncio.create_task(self.p.terminate())
        await asyncio.sleep(0)
        release.set()
        with self.assertRaisesRegex(Exception, '^forbidden$'):
            await reconnect
        await terminate
        self.assertFalse(any(t.running for t in self.tunnels))
        self.assertIsNone(self.p.endpoint)
        self.assertIsNone(self.p.tunnel)

    async def test_concurrent_requests_are_serialized_without_overlap(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(tunnel):
            entered.set()
            await release.wait()
        self.start_hook = block
        payload = self.payload()
        first = asyncio.create_task(self.reconnect(payload))
        await entered.wait()
        second = asyncio.create_task(self.reconnect(payload))
        await asyncio.sleep(0)
        self.assertEqual(len(self.tunnels), 2)
        self.assertFalse(second.done())
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual([action for action, _ in self.calls], ['close', 'start', 'close', 'start'])
        self.assertEqual(sum(t.running for t in self.tunnels), 1)

    async def test_stop_waits_for_reconnect_without_touching_endpoint_early(self):
        host, endpoint = self.p.host, self.p.endpoint
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(tunnel):
            entered.set()
            await release.wait()
        self.start_hook = block
        reconnect = asyncio.create_task(self.reconnect())
        await entered.wait()
        stop = asyncio.create_task(self.p._stop_runtime())
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        self.assert_runtime_kept(host, endpoint)
        release.set()
        await reconnect
        await stop
        self.assertIsNone(self.p.host)
        self.assertIsNone(self.p.tunnel)
        self.assertFalse(any(t.running for t in self.tunnels))

    async def test_unrelated_runtime_warning_survives_success(self):
        self.p.runtime_error = 'storage_busy'
        await self.reconnect()
        self.assertEqual(self.p.runtime_error, 'storage_busy')

    async def test_retired_callback_backend_is_ignored_without_config_rewrite(self):
        # Legacy config is retained, but both startup and reconnect use standard
        # callback behavior and do not expose or apply the retired switch.
        await self.p._stop_runtime()
        self.config['callback_backend'] = 'wechat_httpcore'
        await self.p._start_enabled()
        self.assertTrue(self.p.ready)
        self.assertNotIn('callback_backend', self.p.endpoint.kwargs)
        self.assertNotIn('callback_backend', self.p.admin.status(self.req)['settings'])
        await self.reconnect()
        self.assertEqual(self.config['callback_backend'], 'wechat_httpcore')
        self.assertEqual(self.config.saves, 0)

    async def test_staging_guard_rejects_changed_host_policy_and_source(self):
        event = Event()
        await self.p.on_private_message(event)
        mid = next(iter(self.p.captured))
        host = self.p.host
        staging = host.media_staging
        digest = hashlib.sha256(b'test').hexdigest()
        self.config['private_sender_ids'] = []
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            staging.begin(mid, 'staging-policy', 'file', 'a.txt', 4, digest)
        self.config['private_sender_ids'] = ['200']
        self.p.host = None
        with self.assertRaisesRegex(Exception, '^binding_inactive$'):
            staging.begin(mid, 'staging-host', 'file', 'a.txt', 4, digest)
        self.p.host = host
        event.message_obj.raw_message['message_id'] = 'changed-source'
        with self.assertRaises(Exception):
            staging.begin(mid, 'staging-source', 'file', 'a.txt', 4, digest)
        self.assertEqual(staging.status()['active_uploads'], 0)

    async def test_internal_validated_media_entry_rejects_arbitrary_objects(self):
        media_module = importlib.import_module('plugin_mock.media_reply')
        media = media_module.validate_media('file', 'test.txt', base64.b64encode(b'test').decode())
        class ForeignMedia(media_module.MediaReply):
            pass
        for value in (object(), dict(kind='file'), ForeignMedia(**vars(media))):
            with self.assertRaisesRegex(Exception, '^invalid_media_payload$'):
                await self.p.host.reply_validated_attachment('msg_' + '1' * 32, value, 'key')

    async def test_stop_drains_staged_commit_before_closing_store(self):
        event = Event()
        await self.p.on_private_message(event)
        mid = next(iter(self.p.captured))
        host, db = self.p.host, self.p.host.store.db
        staging = host.media_staging
        data = b'synthetic file data'
        upload = staging.begin(mid, 'staging-drain', 'file', 'test.txt', len(data),
                               hashlib.sha256(data).hexdigest())
        staging.chunk(mid, 'staging-drain', upload['upload_id'], 0,
                      base64.b64encode(data).decode())
        entered = asyncio.Event()
        cleanup_done = []
        async def sender(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
                cleanup_done.append(True)
        host.reply_attachment = sender
        host.reply_validated_attachment = sender
        commit = asyncio.create_task(staging.commit(mid, 'staging-drain', upload['upload_id']))
        await entered.wait()
        await self.p._stop_runtime()
        with self.assertRaises(asyncio.CancelledError):
            await commit
        self.assertEqual(cleanup_done, [True])
        self.assertTrue(staging._closed)
        self.assertEqual(staging._commit_tasks, set())
        with self.assertRaises(Exception):
            db.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
