"""Shared DNS control and legacy callback compatibility; no external requests."""
import copy
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from test_admin import Config
from test_main import load_main
from test_endpoint import endpoint_module, TOKEN
from test_events import events_module, EventError
from astrbot_dot_plugin.proxy_config import ProxySettings


class CallbackDNSAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=False, platform_id='instance-1', adapter='aiocqhttp',
                             self_id='100', sender_id='200', group_ids=['300'],
                             mcp_token='MCP_SECRET_' + 'x' * 40,
                             tunnel_runtime_key='TUNNEL_SECRET_' + 'y' * 40,
                             proxy_username='PROXY_USER_SECRET', proxy_password='PROXY_PASSWORD_SECRET',
                             proxy_url='http://127.0.0.1:7890', proxy_enabled=False,
                             callback_backend='wechat_httpcore')
        # Scope migration is independently covered in test_access_policy.
        self.config.update(access_rules_version=1, private_sender_ids=['200'],
            group_rules=[{'__template_key':'group','group_id':'300','mode':'at',
                          'at_sender_scope':'selected','sender_ids':['200']}])
        self.original = copy.deepcopy(dict(self.config))
        self.plugin = self.module.Main(object(), self.config)
        self.req = types.SimpleNamespace(username='local-admin', plugin_name='astrbot_plugin_dot_private')
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    def payload(self, settings):
        status = self.plugin.admin.status(self.req)
        return {'csrf_token': status['csrf_token'], 'revision': status['revision'], 'settings': settings}

    async def save(self, settings):
        return await self.plugin.admin.mutate(self.req, self.payload(settings), 'settings')

    async def test_old_config_stays_byte_semantically_unchanged_on_load_status_and_stop(self):
        status = self.plugin.admin.status(self.req)
        self.assertEqual(status['settings']['network_dns'], 'system')
        self.assertNotIn('callback_dns', status['settings'])
        self.assertNotIn('attachment_dns', status['settings'])
        self.assertFalse(status['network_dns_conflict'])
        self.assertNotIn('callback_backend', status['settings'])
        self.assertEqual(dict(self.config), self.original)
        await self.plugin._stop_runtime()
        self.assertEqual(dict(self.config), self.original)
        self.assertNotIn('callback_dns', self.config)

    async def test_select_roundtrip_keeps_all_existing_values_and_credentials(self):
        self.config['proxy_enabled'] = True
        original = copy.deepcopy(dict(self.config))
        for mode in ('google', 'system'):
            result = await self.save({'network_dns': mode})
            self.assertEqual(result['settings']['network_dns'], mode)
            self.assertEqual(self.config.saved['network_dns'], mode)
            self.assertFalse(self.plugin.ready)
            for key, value in original.items():
                self.assertEqual(self.config[key], value, key)
            for key in ('mcp_token', 'tunnel_runtime_key', 'proxy_username', 'proxy_password'):
                self.assertNotIn(self.config[key], str(result))
            self.assertNotIn('callback_backend', result['settings'])

    async def test_google_requires_explicit_boolean_proxy_and_rejects_before_runtime_stop_or_save(self):
        for value in (False, None, 'true', 1):
            before = copy.deepcopy(dict(self.config))
            with patch.object(self.plugin, '_stop_runtime') as stop:
                with self.assertRaisesRegex(Exception, '^network_dns_requires_proxy$'):
                    await self.save({'network_dns': 'google', 'proxy_enabled': value})
                stop.assert_not_called()
            self.assertEqual(dict(self.config), before)
            self.assertFalse(hasattr(self.config, 'saved'))
        self.config['proxy_url'] = ''
        with self.assertRaisesRegex(Exception, '^invalid_proxy_settings$'):
            await self.save({'network_dns': 'google', 'proxy_enabled': True, 'proxy_url': ''})
        self.assertFalse(hasattr(self.config, 'saved'))

    async def test_enabled_google_mode_cannot_silently_disable_proxy(self):
        await self.save({'network_dns': 'google', 'proxy_enabled': True})
        with self.assertRaisesRegex(Exception, '^network_dns_requires_proxy$'):
            await self.save({'proxy_enabled': False})
        self.assertTrue(self.config['proxy_enabled'])
        self.assertEqual(self.config['network_dns'], 'google')
        await self.save({'network_dns': 'system', 'proxy_enabled': False})
        self.assertFalse(self.config['proxy_enabled'])

    async def test_unknown_dns_is_rejected_and_status_never_reflects_raw_value(self):
        for value in ('SECRET_DNS_SENTINEL', '', 'GOOGLE', 'google ', None, False, [], {}):
            with self.assertRaisesRegex(Exception, '^invalid_network_dns$'):
                await self.save({'network_dns': value})
            self.config['network_dns'] = value
            status = self.plugin.admin.status(self.req)
            self.assertEqual(status['settings']['network_dns'], '')
            self.assertEqual(status['runtime']['error_code'], 'invalid_network_dns')
            self.assertNotIn('SECRET_DNS_SENTINEL', str(status))
            del self.config['network_dns']
        self.assertFalse(hasattr(self.config, 'saved'))

    async def test_stored_google_without_proxy_status_explains_blocker_without_migration(self):
        self.config['callback_dns'] = 'google'
        before = copy.deepcopy(dict(self.config))
        result = self.plugin.admin.status(self.req)
        self.assertEqual(result['settings']['network_dns'], 'legacy')
        self.assertTrue(result['network_dns_conflict'])
        self.assertEqual(result['runtime']['error_code'], 'callback_dns_requires_proxy')
        self.assertEqual(dict(self.config), before)

    async def test_main_forwards_legacy_dns_and_ignores_backend_without_config_rewrite(self):
        received = {}
        class Endpoint:
            def __init__(self, *args, **kwargs): received.update(kwargs)
            async def start(self): pass
            async def close(self): pass
        self.config.update(enabled=True, proxy_enabled=True, callback_dns='google')
        original = copy.deepcopy(dict(self.config))
        with patch.object(self.module, 'Endpoint', Endpoint):
            await self.plugin._start_enabled()
        self.assertEqual(received['callback_dns'], 'google')
        self.assertNotIn('callback_backend', received)
        self.assertTrue(received['proxy'].enabled)
        self.assertEqual(dict(self.config), original)

    async def test_main_defaults_dns_only_when_absent(self):
        received = {}
        class Endpoint:
            def __init__(self, *args, **kwargs): received.update(kwargs)
            async def start(self): pass
            async def close(self): pass
        self.config['enabled'] = True
        with patch.object(self.module, 'Endpoint', Endpoint):
            await self.plugin._start_enabled()
        self.assertEqual(received['callback_dns'], 'system')
        self.assertNotIn('callback_dns', self.config)


class CallbackDNSRuntimeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_passes_mode_to_events_and_safe_status(self):
        db = sqlite3.connect(':memory:')
        host = types.SimpleNamespace(store=types.SimpleNamespace(db=db))
        endpoint = endpoint_module.Endpoint(host, TOKEN, ['callbacks.example.com'], port=0,
                proxy=ProxySettings(True, 'http://127.0.0.1:7890'), callback_dns='google')
        try:
            await endpoint.start()
            self.assertEqual(endpoint.events.transport.callback_dns, 'google')
            self.assertEqual(endpoint.events.transport.backend, 'aiohttp')
            self.assertEqual(endpoint.events.status()['callback_dns'], 'google')
            endpoint.events.transport.callback_dns = 'SECRET_DNS_SENTINEL'
            self.assertEqual(endpoint.events.status()['callback_dns'], '')
            self.assertNotIn('SECRET_DNS_SENTINEL', str(endpoint.events.status()))
        finally:
            await endpoint.close()
            db.close()

    async def test_endpoint_ignores_any_retired_backend_value(self):
        for legacy in ('wechat_httpcore', 'aiohttp', 'SECRET_BACKEND', None, True, [], {}):
            with self.subTest(legacy=legacy):
                db = sqlite3.connect(':memory:')
                host = types.SimpleNamespace(store=types.SimpleNamespace(db=db))
                endpoint = endpoint_module.Endpoint(host, TOKEN, port=0, callback_backend=legacy)
                try:
                    await endpoint.start()
                    self.assertEqual(endpoint.callback_backend, 'aiohttp')
                    self.assertEqual(endpoint.events.transport.backend, 'aiohttp')
                    self.assertEqual(endpoint.events.status()['callback_backend'], 'aiohttp')
                    self.assertNotIn('SECRET_BACKEND', str(endpoint.events.status()))
                finally:
                    await endpoint.close()
                    db.close()

    async def test_unknown_endpoint_dns_fails_closed(self):
        for value in ('SECRET_DNS_SENTINEL', '', None, [], {}):
            with self.assertRaisesRegex(EventError, '^invalid_callback_dns$'):
                endpoint_module.Endpoint(None, TOKEN, callback_dns=value)

    async def test_events_default_and_explicit_google_without_proxy(self):
        db = sqlite3.connect(':memory:')
        try:
            events = events_module.Events(db, 'owner', ['callbacks.example.com'])
            self.assertEqual(events.status()['callback_dns'], 'system')
            await events.transport.close()
            with self.assertRaisesRegex(EventError, '^callback_dns_requires_proxy$'):
                events_module.Events(db, 'owner', ['callbacks.example.com'], callback_dns='google')
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
