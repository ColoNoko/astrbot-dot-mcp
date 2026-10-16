"""Retired context setting compatibility; synthetic configuration, no network."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest

from test_admin import Config
from test_main import load_main


LEGACY_VALUES = (0, 3, 8, True, False, -1, 9, 1.0, '8', '', None, [], {},
                 'SECRET_CONTEXT_VALUE')


class ChatContextConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=False, platform_id='instance-1', adapter='aiocqhttp',
                             self_id='100', sender_id='200', group_ids=['300'],
                             group_trigger_mode='all_messages', callback_hosts=['callbacks.example.com'],
                             callback_backend='wechat_httpcore', callback_dns='google',
                             attachment_dns='google', attachment_private_http=True,
                             mcp_token='MCP_SECRET_' + 'x' * 40,
                             tunnel_runtime_key='TUNNEL_SECRET_' + 'y' * 40,
                             proxy_username='PROXY_USER_SECRET', proxy_password='PROXY_PASSWORD_SECRET',
                             proxy_url='http://127.0.0.1:7890', proxy_enabled=True)
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

    async def test_missing_key_is_not_exposed_or_created_on_get_stop_or_save(self):
        for _ in range(2):
            result = self.plugin.admin.status(self.req)
            self.assertNotIn('chat_context_messages', result['settings'])
            self.assertEqual(dict(self.config), self.original)
        await self.plugin._stop_runtime()
        self.assertEqual(dict(self.config), self.original)
        self.assertFalse(hasattr(self.config, 'saved'))
        result = await self.save({'listen_port': 8770})
        self.assertNotIn('chat_context_messages', result['settings'])
        self.assertNotIn('chat_context_messages', self.config.saved)
        self.assertFalse(self.plugin.ready)

    async def test_old_eight_is_hidden_ignored_and_preserved_during_unrelated_save(self):
        self.config['chat_context_messages'] = 8
        result = await self.save({'listen_port': 8770})
        self.assertEqual(self.config.saved['chat_context_messages'], 8)
        self.assertNotIn('chat_context_messages', result['settings'])
        self.assertIsNone(result['runtime']['error_code'])
        for key, value in self.original.items():
            self.assertEqual(self.config[key], value, key)
        for key in ('mcp_token', 'tunnel_runtime_key', 'proxy_username', 'proxy_password'):
            self.assertNotIn(self.config[key], json.dumps(result))

    async def test_all_stored_legacy_values_are_ignored_without_get_or_save_data_loss(self):
        for value in LEGACY_VALUES:
            with self.subTest(value=value):
                self.config['chat_context_messages'] = copy.deepcopy(value)
                before = copy.deepcopy(dict(self.config))
                result = self.plugin.admin.status(self.req)
                self.assertNotIn('chat_context_messages', result['settings'])
                self.assertIsNone(result['runtime']['error_code'])
                self.assertNotIn('SECRET_CONTEXT_VALUE', json.dumps(result))
                self.assertEqual(dict(self.config), before)
                result = await self.save({'listen_port': 8770})
                self.assertEqual(self.config.saved['chat_context_messages'], value)
                self.assertNotIn('chat_context_messages', result['settings'])
                self.assertIsNone(result['runtime']['error_code'])

    async def test_stale_client_cannot_reactivate_replace_or_add_retired_setting(self):
        for stored in (None, 8, 'SECRET_CONTEXT_VALUE'):
            if stored is None:
                self.config.pop('chat_context_messages', None)
            else:
                self.config['chat_context_messages'] = stored
            for value in LEGACY_VALUES:
                with self.subTest(stored=stored, value=value):
                    result = await self.save({'chat_context_messages': value})
                    self.assertNotIn('chat_context_messages', result['settings'])
                    self.assertIsNone(result['runtime']['error_code'])
                    if stored is None:
                        self.assertNotIn('chat_context_messages', self.config.saved)
                    else:
                        self.assertEqual(self.config.saved['chat_context_messages'], stored)

    async def test_legacy_values_never_block_startup_or_register_history_callback(self):
        self.config['enabled'] = True
        for value in LEGACY_VALUES:
            with self.subTest(value=value):
                self.config['chat_context_messages'] = copy.deepcopy(value)
                await self.plugin._start_enabled()
                self.assertTrue(self.plugin.ready)
                self.assertIsNone(self.plugin.runtime_error)
                self.assertFalse(hasattr(self.plugin.host, 'read_context'))
                self.assertFalse(hasattr(self.plugin, '_read_context'))
                self.assertTrue(callable(self.plugin.host.validate_read_message))
                self.assertEqual(self.config['chat_context_messages'], value)
                await self.plugin._stop_runtime()

    async def test_unknown_setting_still_rejected(self):
        with self.assertRaisesRegex(Exception, '^invalid_settings$'):
            await self.save({'chat_context_messages_typo': 8})
        self.assertFalse(hasattr(self.config, 'saved'))


class ChatContextSchemaTests(unittest.TestCase):
    def test_retired_key_stays_hidden_with_zero_default_and_other_schema_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / '_conf_schema.json').read_text())
        from schema_contract import assert_legacy_schema
        assert_legacy_schema(self, schema)
        retired = schema['chat_context_messages']
        self.assertIn('已停用', retired['description'])
        self.assertEqual(retired['type'], 'int')
        self.assertEqual(retired['default'], 0)
        self.assertIs(retired['invisible'], True)
        self.assertIn('此值不再生效', retired['hint'])
        self.assertLessEqual(len(retired['hint']), 150)


if __name__ == '__main__':
    unittest.main()
