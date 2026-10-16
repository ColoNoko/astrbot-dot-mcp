"""Default-off configuration and old-config inheritance; no external HTTP."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from test_admin import Config
from test_main import load_main


class AttachmentHTTPConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_main(self.tmp.name)
        self.config = Config(enabled=False, platform_id='instance-1', adapter='aiocqhttp',
                             self_id='100', sender_id='200', group_ids=['300'],
                             mcp_token='KEEP_SECRET_' + 'x' * 32, callback_dns='google',
                             proxy_enabled=True, proxy_url='http://127.0.0.1:7890')
        # Scope migration is independently covered in test_access_policy.
        self.config.update(access_rules_version=1, private_sender_ids=['200'],
            group_rules=[{'__template_key':'group','group_id':'300','mode':'at',
                          'at_sender_scope':'selected','sender_ids':['200']}])
        self.original = copy.deepcopy(dict(self.config))
        self.plugin = self.module.Main(object(), self.config)
        await self.plugin.initialize()
        self.req = types.SimpleNamespace(username='local-admin', plugin_name='astrbot_plugin_dot_private')

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def save(self, settings):
        status = self.plugin.admin.status(self.req)
        return await self.plugin.admin.mutate(self.req, {'csrf_token':status['csrf_token'],
            'revision':status['revision'], 'settings':settings}, 'settings')

    async def test_old_config_status_and_load_do_not_enable_or_write(self):
        status = self.plugin.admin.status(self.req)
        self.assertIs(status['settings']['attachment_private_http'], False)
        self.assertEqual(dict(self.config), self.original)
        self.assertNotIn('attachment_private_http', self.config)
        self.assertFalse(self.plugin.ready)

    async def test_explicit_boolean_save_preserves_binding_and_secrets(self):
        for enabled in (True, False):
            result = await self.save({'attachment_private_http':enabled})
            self.assertIs(self.config['attachment_private_http'], enabled)
            self.assertIs(result['settings']['attachment_private_http'], enabled)
            for key, value in self.original.items():
                self.assertEqual(self.config[key], value)
            self.assertFalse(self.plugin.ready)
            self.assertNotIn('KEEP_SECRET_', json.dumps(result))

    async def test_omitted_setting_preserves_existing_opt_in(self):
        self.config['attachment_private_http'] = True
        await self.save({'self_id':'100'})
        self.assertIs(self.config['attachment_private_http'], True)

    async def test_nonboolean_input_rejected_without_mutation(self):
        for value in (1, 0, 'true', 'false', None, [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, '^invalid_attachment_http_setting$'):
                    await self.save({'attachment_private_http':value})
                self.assertEqual(dict(self.config), self.original)

    async def test_malformed_disk_setting_is_not_enabled_or_echoed(self):
        self.config['attachment_private_http'] = 'SECRET_SETTING'
        result = self.plugin.admin.status(self.req)
        self.assertIs(result['settings']['attachment_private_http'], False)
        self.assertEqual(result['runtime']['error_code'], 'invalid_attachment_http_setting')
        self.assertNotIn('SECRET_SETTING', json.dumps(result))

    async def test_schema_keeps_all_previous_keys_and_default_is_false(self):
        import hashlib
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root/'_conf_schema.json').read_text())
        from schema_contract import assert_legacy_schema
        assert_legacy_schema(self, schema)
        added = schema['attachment_private_http']
        self.assertEqual(added['type'], 'bool')
        self.assertIs(added['default'], False)
        self.assertIs(added['invisible'], True)
