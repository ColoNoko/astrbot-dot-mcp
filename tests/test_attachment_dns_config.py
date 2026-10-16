"""Shared DNS setting and legacy attachment migration; no external queries."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_admin import Config
from test_main import load_main


class AttachmentDNSAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=False, platform_id='instance-1', adapter='aiocqhttp',
                             self_id='100', sender_id='200', group_ids=['300'],
                             group_trigger_mode='all_messages', callback_hosts=['callbacks.example.com'],
                             callback_backend='wechat_httpcore', callback_dns='system',
                             attachment_private_http=False,
                             mcp_token='MCP_SECRET_' + 'x' * 40,
                             tunnel_runtime_key='TUNNEL_SECRET_' + 'y' * 40,
                             proxy_username='PROXY_USER_SECRET', proxy_password='PROXY_PASSWORD_SECRET',
                             proxy_url='http://127.0.0.1:7890', proxy_enabled=False)
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

    async def save(self, settings):
        status = self.plugin.admin.status(self.req)
        return await self.plugin.admin.mutate(self.req, {
            'csrf_token': status['csrf_token'], 'revision': status['revision'],
            'settings': settings}, 'settings')

    async def test_old_config_load_repeated_get_and_stop_do_not_write(self):
        for _ in range(2):
            status = self.plugin.admin.status(self.req)
            self.assertEqual(status['settings']['network_dns'], 'system')
            self.assertNotIn('attachment_dns', status['settings'])
            self.assertNotIn('callback_dns', status['settings'])
            self.assertFalse(status['network_dns_conflict'])
            self.assertNotIn('callback_backend', status['settings'])
            self.assertIs(status['settings']['attachment_private_http'], False)
            self.assertEqual(dict(self.config), self.original)
        await self.plugin._stop_runtime()
        self.assertEqual(dict(self.config), self.original)
        self.assertNotIn('attachment_dns', self.config)
        self.assertFalse(hasattr(self.config, 'saved'))

    async def test_mode_roundtrip_preserves_all_old_values_and_redacts_credentials(self):
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
                self.assertNotIn(self.config[key], json.dumps(result))

    async def test_legacy_modes_remain_independent_and_conflict_is_visible_without_writes(self):
        self.config['proxy_enabled'] = True
        for callback in ('system', 'google'):
            for attachment in ('google', 'system'):
                self.config.update(callback_dns=callback, attachment_dns=attachment)
                before = copy.deepcopy(dict(self.config))
                result = self.plugin.admin.status(self.req)
                self.assertEqual(result['settings']['network_dns'], callback if callback == attachment else 'legacy')
                self.assertEqual(result['network_dns_conflict'], callback != attachment)
                self.assertNotIn('callback_dns', result['settings'])
                self.assertNotIn('attachment_dns', result['settings'])
                self.assertEqual(dict(self.config), before)
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_omitted_save_preserves_current_google_and_other_settings(self):
        self.config.update(attachment_dns='google', proxy_enabled=True, attachment_private_http=True)
        original = copy.deepcopy(dict(self.config))
        for settings in ({}, {'listen_port': 8770}):
            result = await self.save(settings)
            self.assertEqual(result['settings']['network_dns'], 'legacy')
            self.assertTrue(result['network_dns_conflict'])
            self.assertEqual(self.config.saved['attachment_dns'], 'google')
            for key, value in original.items():
                self.assertEqual(self.config[key], value, key)

    async def test_stale_dns_controls_cannot_overwrite_saved_modes(self):
        self.config.update(callback_dns='system', attachment_dns='google', proxy_enabled=True)
        for key in ('callback_dns', 'attachment_dns'):
            for value in ('SECRET_OLD_DNS', '', 'system', 'google', None, False, True, 0, [], {}):
                with self.subTest(key=key, value=value):
                    result = await self.save({key: value})
                    self.assertEqual(self.config.saved['callback_dns'], 'system')
                    self.assertEqual(self.config.saved['attachment_dns'], 'google')
                    self.assertEqual(result['settings']['network_dns'], 'legacy')
                    self.assertTrue(result['network_dns_conflict'])
                    self.assertNotIn(key, result['settings'])
                    self.assertNotIn('SECRET_OLD_DNS', json.dumps(result))

    async def test_malformed_saved_values_are_redacted_without_defaulting_or_mutation(self):
        for value in ('SECRET_ATTACHMENT_DNS', '', 'GOOGLE', 'google ', None, False, 1, [], {}):
            with self.subTest(value=value):
                self.config['attachment_dns'] = value
                before = copy.deepcopy(dict(self.config))
                result = self.plugin.admin.status(self.req)
                self.assertEqual(result['settings']['network_dns'], '')
                self.assertEqual(result['runtime']['error_code'], 'invalid_attachment_dns')
                self.assertNotIn('SECRET_ATTACHMENT_DNS', json.dumps(result))
                self.assertEqual(dict(self.config), before)
                with patch.object(self.plugin, '_stop_runtime') as stop:
                    with self.assertRaisesRegex(Exception, '^invalid_attachment_dns$'):
                        await self.save({'listen_port': 8770})
                    stop.assert_not_called()
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_google_requires_explicit_boolean_proxy_before_stop_or_save(self):
        for value in (False, None, 'true', 0, 1, [], {}):
            with self.subTest(value=value), patch.object(self.plugin, '_stop_runtime') as stop:
                with self.assertRaisesRegex(Exception, '^network_dns_requires_proxy$'):
                    await self.save({'network_dns': 'google', 'proxy_enabled': value})
                stop.assert_not_called()
                self.assertEqual(dict(self.config), self.original)
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_google_requires_valid_configured_proxy(self):
        for url in ('', 'https://proxy.example:7890', 'http://secret:token@proxy.example:7890'):
            with self.subTest(url=url), patch.object(self.plugin, '_stop_runtime') as stop:
                # Keep the destination unchanged so existing credential re-entry
                # validation does not precede the proxy validity check under test.
                self.config['proxy_url'] = url
                before = copy.deepcopy(dict(self.config))
                with self.assertRaisesRegex(Exception, '^invalid_proxy_settings$'):
                    await self.save({'network_dns': 'google', 'proxy_enabled': True,
                                     'proxy_url': url})
                stop.assert_not_called()
                self.assertEqual(dict(self.config), before)
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_google_cannot_disable_proxy_until_explicitly_changed_to_system(self):
        await self.save({'network_dns': 'google', 'proxy_enabled': True})
        before = copy.deepcopy(dict(self.config))
        with patch.object(self.plugin, '_stop_runtime') as stop:
            with self.assertRaisesRegex(Exception, '^network_dns_requires_proxy$'):
                await self.save({'proxy_enabled': False})
            stop.assert_not_called()
        self.assertEqual(dict(self.config), before)
        result = await self.save({'network_dns': 'system', 'proxy_enabled': False})
        self.assertEqual(result['settings']['network_dns'], 'system')
        self.assertIs(result['settings']['proxy_enabled'], False)

    async def test_saved_google_without_proxy_has_safe_specific_blocker_without_writes(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                self.config.update(attachment_dns='google', proxy_enabled=value)
                before = copy.deepcopy(dict(self.config))
                result = self.plugin.admin.status(self.req)
                self.assertEqual(result['settings']['network_dns'], 'legacy')
                self.assertTrue(result['network_dns_conflict'])
                self.assertEqual(result['runtime']['error_code'], 'attachment_dns_requires_proxy')
                self.assertEqual(dict(self.config), before)
                self.assertFalse(hasattr(self.config, 'saved'))


    async def test_main_defaults_only_when_absent_and_never_queries_dns_on_start(self):
        self.config['enabled'] = True
        before = copy.deepcopy(dict(self.config))
        with patch('plugin_mock.attachments.GoogleResolver._get_session') as session:
            await self.plugin._start_enabled()
            self.assertTrue(self.plugin.ready)
            self.assertEqual(self.plugin.host.store.attachments.transport.attachment_dns, 'system')
            session.assert_not_called()
        self.assertNotIn('attachment_dns', self.config)
        self.assertEqual(dict(self.config), before)
        self.assertFalse(hasattr(self.config, 'saved'))

    async def test_main_preserves_legacy_independent_modes_without_dns_query_or_config_rewrite(self):
        received = {}
        class Endpoint:
            def __init__(self, *args, **kwargs): received.update(kwargs)
            async def start(self): pass
            async def close(self): pass
        for callback in ('system', 'google'):
            for attachment in ('system', 'google'):
                with self.subTest(callback=callback, attachment=attachment):
                    await self.plugin._stop_runtime()
                    self.config.update(enabled=True, proxy_enabled=True,
                                       callback_dns=callback, attachment_dns=attachment)
                    before = copy.deepcopy(dict(self.config))
                    with patch.object(self.module, 'Endpoint', Endpoint), \
                            patch('plugin_mock.attachments.GoogleResolver._get_session') as session:
                        await self.plugin._start_enabled()
                        self.assertTrue(self.plugin.ready)
                        self.assertEqual(self.plugin.host.store.attachments.transport.attachment_dns, attachment)
                        self.assertEqual(received['callback_dns'], callback)
                        self.assertNotIn('callback_backend', received)
                        self.assertTrue(self.plugin.host.store.attachments.transport.proxy.enabled)
                        session.assert_not_called()
                    self.assertEqual(dict(self.config), before)
                    self.assertFalse(hasattr(self.config, 'saved'))

    async def test_explicit_shared_mode_overrides_both_legacy_modes_without_queries_or_rewrite(self):
        received = {}
        class Endpoint:
            def __init__(self, *args, **kwargs): received.update(kwargs)
            async def start(self): pass
            async def close(self): pass
        for mode in ('system', 'google'):
            for callback, attachment in (('system', 'google'), ('google', 'system'),
                                         ('INVALID_OLD_DNS', None)):
                with self.subTest(mode=mode, callback=callback, attachment=attachment):
                    await self.plugin._stop_runtime()
                    self.config.update(enabled=True, proxy_enabled=True, network_dns=mode,
                                       callback_dns=callback, attachment_dns=attachment)
                    before = copy.deepcopy(dict(self.config))
                    with patch.object(self.module, 'Endpoint', Endpoint), \
                         patch('plugin_mock.attachments.GoogleResolver._get_session') as session:
                        await self.plugin._start_enabled()
                        self.assertTrue(self.plugin.ready)
                        self.assertEqual(self.plugin.host.store.attachments.transport.attachment_dns, mode)
                        self.assertEqual(received['callback_dns'], mode)
                        self.assertNotIn('callback_backend', received)
                        session.assert_not_called()
                    result = self.plugin.admin.status(self.req)
                    self.assertEqual(result['settings']['network_dns'], mode)
                    self.assertFalse(result['network_dns_conflict'])
                    self.assertIsNone(result['runtime']['error_code'])
                    self.assertNotIn('INVALID_OLD_DNS', json.dumps(result))
                    self.assertEqual(dict(self.config), before)
                    self.assertFalse(hasattr(self.config, 'saved'))

    async def test_invalid_shared_mode_blocks_startup_without_dns_or_config_mutation(self):
        for value in ('SECRET_NETWORK_DNS', '', 'GOOGLE', None, False, 1, [], {}):
            with self.subTest(value=value):
                self.config.update(enabled=True, network_dns=value)
                before = copy.deepcopy(dict(self.config))
                with patch.object(self.module, 'Endpoint') as endpoint, \
                     patch('plugin_mock.attachments.GoogleResolver._get_session') as session:
                    with self.assertRaisesRegex(RuntimeError, '^dot_private_startup_failed$'):
                        await self.plugin._start_enabled()
                    endpoint.assert_not_called()
                    session.assert_not_called()
                self.assertFalse(self.plugin.ready)
                self.assertIsNone(self.plugin.host)
                self.assertEqual(self.plugin.runtime_error, 'invalid_network_dns')
                self.assertEqual(dict(self.config), before)
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_main_malformed_manual_config_fails_closed_without_rewriting_it(self):
        for value in ('SECRET_ATTACHMENT_DNS', '', 'GOOGLE', None, False, 1, [], {}):
            with self.subTest(value=value), patch.object(self.module, 'Endpoint') as endpoint:
                self.config.update(enabled=True, attachment_dns=value)
                before = copy.deepcopy(dict(self.config))
                with self.assertRaisesRegex(RuntimeError, '^dot_private_startup_failed$'):
                    await self.plugin._start_enabled()
                self.assertFalse(self.plugin.ready)
                self.assertIsNone(self.plugin.host)
                self.assertIsNone(self.plugin.endpoint)
                self.assertEqual(self.plugin.runtime_error, 'invalid_attachment_dns')
                endpoint.assert_not_called()
                result = self.plugin.admin.status(self.req)
                self.assertEqual(result['runtime']['error_code'], 'invalid_attachment_dns')
                self.assertNotIn('SECRET_ATTACHMENT_DNS', json.dumps(result))
                self.assertEqual(dict(self.config), before)
                self.assertFalse(hasattr(self.config, 'saved'))

    async def test_main_manual_google_without_proxy_fails_closed_without_queries(self):
        self.config.update(enabled=True, attachment_dns='google', proxy_enabled=False)
        before = copy.deepcopy(dict(self.config))
        with patch.object(self.module, 'Endpoint') as endpoint, \
                patch('plugin_mock.attachments.GoogleResolver._get_session') as session:
            with self.assertRaisesRegex(RuntimeError, '^dot_private_startup_failed$'):
                await self.plugin._start_enabled()
            endpoint.assert_not_called()
            session.assert_not_called()
        self.assertFalse(self.plugin.ready)
        self.assertIsNone(self.plugin.host)
        self.assertEqual(self.plugin.runtime_error, 'attachment_dns_requires_proxy')
        self.assertEqual(dict(self.config), before)
        self.assertFalse(hasattr(self.config, 'saved'))


class AttachmentDNSSchemaTests(unittest.TestCase):
    def test_one_shared_dns_key_replaces_retired_per_path_controls(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / '_conf_schema.json').read_text())
        from schema_contract import assert_legacy_schema
        assert_legacy_schema(self, schema)
        for key in ('callback_backend', 'callback_dns', 'attachment_dns'):
            self.assertIs(schema[key]['invisible'], True)
        self.assertNotIn('options', schema['callback_backend'])
        self.assertNotIn('labels', schema['callback_backend'])
        added = schema['network_dns']
        self.assertEqual(added['type'], 'string')
        self.assertEqual(added['default'], 'legacy')
        self.assertEqual(added['options'], ['legacy', 'system', 'google'])
        self.assertEqual(len(added['labels']), len(added['options']))
        self.assertTrue(all(isinstance(label, str) and label for label in added['labels']))
        self.assertIs(added['invisible'], True)
        self.assertLessEqual(len(added['hint']), 150)


if __name__ == '__main__':
    unittest.main()
