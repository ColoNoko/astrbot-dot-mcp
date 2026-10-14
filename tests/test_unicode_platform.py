import tempfile
import unittest
from test_discovery import Config, Context, Instance
from discovery import discover_defaults, valid_platform_id
from test_main import load_main, Event

class UnicodeDiscoveryTests(unittest.TestCase):
    def test_chinese_metadata_and_config_fill_exactly(self):
        config=Config(enabled=False,platform_id='',adapter='')
        result=discover_defaults(Context([Instance('示例机器人')]),config)
        self.assertEqual(result['state'],'saved')
        self.assertEqual(config['platform_id'],'示例机器人')
    def test_multiplatform_chinese_never_selects_first(self):
        config=Config(platform_id='',adapter='')
        result=discover_defaults(Context([Instance('示例机器人'),Instance('另一个机器人')]),config)
        self.assertEqual(result['state'],'multiple_candidates')
        self.assertEqual(config['platform_id'],'')
    def test_explicit_chinese_selection_matches_exact_instance(self):
        context=Context([Instance('示例机器人'),Instance('其他')]); config=Config(platform_id='示例机器人',adapter='')
        self.assertEqual(discover_defaults(context,config)['state'],'saved')
        self.assertEqual(context.lookups,['示例机器人'])
    def test_id_guard_unicode_and_controls(self):
        for value in ('示例机器人','é', 'e\u0301', '🤖'*128):
            self.assertTrue(valid_platform_id(value),repr(value))
        for value in ('',' ', '机器人 一号', '<script>', 'https://host/?token=secret', ' a','a ', 'a\n', 'a\x7f', 'a\u200b', 'a\u202e', '\ud800', '🤖'*129,True):
            self.assertFalse(valid_platform_id(value),repr(value))
        self.assertNotEqual('é','e\u0301')  # No normalization merges distinct identities.

class UnicodeAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_save_and_capture_keep_chinese_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            module=load_main(tmp); context=Context([Instance('示例机器人')])
            config=Config(enabled=False,platform_id='',adapter='',self_id='100',sender_id='200',mcp_token='DUMMY_'+('x'*40))
            plugin=module.Main(context,config); await plugin.initialize()
            from types import SimpleNamespace
            req=SimpleNamespace(username='admin',plugin_name='astrbot_plugin_dot_private')
            status=plugin.admin.status(req)
            self.assertEqual(status['platforms'],[{'id':'示例机器人','adapter':'aiocqhttp'}])
            await plugin.admin.mutate(req,{'csrf_token':status['csrf_token'],'revision':status['revision'],
                'settings':{'platform_id':'示例机器人','adapter':'aiocqhttp'}},'settings')
            self.assertEqual(config['platform_id'],'示例机器人')
            config['enabled']=True; await plugin._start_enabled()
            event=Event(); event.platform='示例机器人'
            # Base fixture's platform accessor is backed by platform_id.
            event.get_platform_id=lambda:'示例机器人'
            await plugin.on_private_message(event)
            self.assertTrue(event.stopped)
            other=Event(); other.get_platform_id=lambda:'示例机器人2'
            await plugin.on_private_message(other); self.assertFalse(other.stopped)
            await plugin.terminate()
