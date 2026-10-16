"""Release compatibility invariants, not a live AstrBot installer test."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from test_main import load_main, Event
from test_groups import group_event

ROOT=Path(__file__).resolve().parents[1]
OLD_SCHEMA_KEYS={'enabled','platform_id','adapter','self_id','sender_id','mcp_token',
                 'callback_hosts','listen_host','listen_port','group_ids'}

class UpgradeContractTests(unittest.TestCase):
    def test_all_existing_schema_keys_and_types_are_preserved(self):
        schema=json.loads((ROOT/'_conf_schema.json').read_text())
        self.assertTrue(OLD_SCHEMA_KEYS <= schema.keys())
        for key in OLD_SCHEMA_KEYS:
            expected={'enabled':'bool','callback_hosts':'list','group_ids':'list','listen_port':'int'}.get(key,'string')
            self.assertEqual(schema[key]['type'],expected)
        self.assertEqual(schema['group_trigger_mode']['default'],'owner_at')
        self.assertEqual(schema['group_ids']['default'],[])
        self.assertFalse(schema['enabled']['default'])
    def test_plugin_identity_storage_path_and_version_are_stable(self):
        metadata=(ROOT/'metadata.yaml').read_text()
        self.assertEqual(re.search(r'^name:\s*(\S+)',metadata,re.M).group(1),'astrbot_plugin_dot_private')
        self.assertIn('version: v0.5.6',metadata)
        version=re.search(r'^version:\s*v(\S+)',metadata,re.M).group(1)
        self.assertIn('PLUGIN_VERSION = '+json.dumps(version),(ROOT/'endpoint.py').read_text())
        self.assertIn("StarTools.get_data_dir('astrbot_plugin_dot_private')",(ROOT/'main.py').read_text())

class ExistingConfigAndDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_v020_shaped_config_and_db_reopen_without_scope_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            module=load_main(tmp)
            config={'enabled':True,'platform_id':'instance-1','adapter':'aiocqhttp',
                'self_id':'100','sender_id':'200','mcp_token':'synthetic-'+'x'*32,
                'callback_hosts':['callbacks.example.com'],'listen_host':'127.0.0.1',
                'listen_port':8769,'group_ids':['300','400']}
            original=dict(config)
            first=module.Main(object(),config); await first.initialize()
            private=Event(); await first.on_private_message(private)
            private_mid=next(iter(first.captured))
            group=group_event(); await first.on_group_message(group)
            group_mid=next(reversed(first.captured))
            await first.host.reply(group_mid,'old reply','existing-key')
            await first.terminate()
            upgraded=module.Main(object(),config); await upgraded.initialize()
            try:
                self.assertEqual(config,original)
                self.assertNotIn('group_trigger_mode',config)
                self.assertNotIn('callback_dns',config)
                self.assertNotIn('callback_backend',config)
                self.assertEqual(upgraded._group_mode(),'owner_at')
                self.assertEqual(upgraded.host.store.read(private_mid)['privacy_scope'],'private_owner_only')
                self.assertEqual(upgraded.host.store.read(group_mid)['group_id'],'300')
                self.assertEqual((await upgraded.host.reply(group_mid,'old reply','existing-key'))['state'],'adapter_completed')
                replay=group_event(); await upgraded.on_group_message(replay)
                self.assertEqual(upgraded.host.store.status()['messages'],2)
                self.assertEqual(replay.sent,[])
                self.assertEqual(upgraded.host.store.db.execute('PRAGMA user_version').fetchone()[0],3)
            finally: await upgraded.terminate()

if __name__=='__main__': unittest.main()
