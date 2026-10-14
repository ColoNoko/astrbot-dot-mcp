import copy
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from discovery import discover_defaults, MAX_CANDIDATES, MAX_ID_LENGTH


class Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
        self.saved = None
        self.fail_save = False

    def save_config(self):
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError('token=secret configuration=private')
        self.saved = copy.deepcopy(dict(self))


class Instance:
    def __init__(self, platform_id='qq-main', name='aiocqhttp'):
        self.metadata = SimpleNamespace(id=platform_id, name=name)

    def meta(self):
        return self.metadata

    @property
    def client(self):
        raise AssertionError('must not access the network client')

    @property
    def client_self_id(self):
        raise AssertionError('client_self_id is not a QQ account ID')


class Context:
    def __init__(self, instances=()):
        self.instances = list(instances)
        self.platform_manager = SimpleNamespace(get_insts=lambda: self.instances)
        self.lookups = []

    def get_config(self):
        return {'platform':[{'id':i.meta().id,'type':i.meta().name,'enable':True} for i in self.instances]}

    def get_platform_inst(self, platform_id):
        self.lookups.append(platform_id)
        return next((instance for instance in self.instances
                     if instance.meta().id == platform_id), None)


class DiscoveryTests(unittest.TestCase):
    def test_zero_candidates_leaves_config_untouched(self):
        for instances in ([], [Instance('discord', 'discord')]):
            config = Config(platform_id='', adapter='', enabled=False)
            original = dict(config)
            result = discover_defaults(Context(instances), config)
            self.assertEqual(result, {'state': 'no_candidates', 'candidate_ids': []})
            self.assertEqual(config, original)
            self.assertEqual(config.save_calls, 0)

    def test_unique_candidate_fills_only_blanks_and_persists_once(self):
        context = Context([Instance('other', 'discord'), Instance('qq-main')])
        config = Config(platform_id='', adapter='', enabled=False, self_id='',
                        sender_id='', allowed_group_ids=[], mcp_token='',
                        listen_host='127.0.0.1', listen_port=8769)
        original = dict(config)
        result = discover_defaults(context, config)
        self.assertEqual(result, {'state': 'saved', 'candidate_ids': ['qq-main']})
        self.assertEqual(config, dict(original, platform_id='qq-main', adapter='aiocqhttp'))
        self.assertEqual(config.saved, dict(config))
        self.assertEqual(config.save_calls, 1)
        self.assertEqual(context.lookups, [])
        self.assertEqual(discover_defaults(context, config)['state'], 'unchanged')
        self.assertEqual(config.save_calls, 1)

    def test_failed_second_configured_adapter_blocks_single_loaded_default(self):
        context=Context([Instance('qq-main')])
        context.get_config=lambda:{'platform':[{'id':'qq-main','type':'aiocqhttp','enable':True},
            {'id':'qq-failed','type':'aiocqhttp','enable':True,'ws_reverse_token':'never-expose'}]}
        config=Config(platform_id='',adapter='')
        result=discover_defaults(context,config)
        self.assertEqual(result['state'],'configured_ambiguity')
        self.assertEqual(config,{'platform_id':'','adapter':''}); self.assertEqual(config.save_calls,0)
        self.assertNotIn('never-expose',str(result))
    def test_missing_configured_manifest_fails_soft(self):
        context=Context([Instance()]); context.get_config=None
        config=Config(platform_id='',adapter='')
        self.assertEqual(discover_defaults(context,config)['state'],'unavailable')
        self.assertEqual(config.save_calls,0)
    def test_missing_keys_can_be_filled(self):
        config = Config()
        self.assertEqual(discover_defaults(Context([Instance()]), config)['state'], 'saved')
        self.assertEqual(config, {'platform_id': 'qq-main', 'adapter': 'aiocqhttp'})

    def test_multiple_candidates_never_choose_first(self):
        config = Config(platform_id='', adapter='')
        result = discover_defaults(Context([Instance('qq-a'), Instance('qq-b')]), config)
        self.assertEqual(result, {'state': 'multiple_candidates',
                                  'candidate_ids': ['qq-a', 'qq-b']})
        self.assertEqual(config, {'platform_id': '', 'adapter': ''})
        self.assertEqual(config.save_calls, 0)

    def test_explicit_platform_matches_exactly_among_multiple_candidates(self):
        context = Context([Instance('qq'), Instance('qq-2')])
        config = Config(platform_id='qq-2', adapter='')
        result = discover_defaults(context, config)
        self.assertEqual(result['state'], 'saved')
        self.assertEqual(config, {'platform_id': 'qq-2', 'adapter': 'aiocqhttp'})
        self.assertEqual(context.lookups, ['qq-2'])
        missing = Config(platform_id='qq-', adapter='')
        self.assertEqual(discover_defaults(context, missing)['state'], 'not_found')
        self.assertEqual(missing['adapter'], '')

    def test_existing_adapter_allows_only_platform_fill(self):
        config = Config(platform_id='', adapter='aiocqhttp')
        self.assertEqual(discover_defaults(Context([Instance()]), config)['state'], 'saved')
        self.assertEqual(config['adapter'], 'aiocqhttp')

    def test_nonblank_whitespace_and_malformed_user_edits_are_preserved(self):
        for key in ('platform_id', 'adapter'):
            for value in (' ', '\t', 'manual-value', None, False, 17, ['manual']):
                config = Config(platform_id='', adapter='')
                config[key] = value
                original = copy.deepcopy(dict(config))
                with self.subTest(key=key, value=value):
                    discover_defaults(Context([Instance()]), config)
                    self.assertEqual(config, original)
                    self.assertEqual(config.save_calls, 0)

    def test_all_explicit_values_stay_untouched(self):
        config = Config(platform_id='my-selection', adapter='aiocqhttp')
        self.assertEqual(discover_defaults(object(), config)['state'], 'unchanged')
        self.assertEqual(config.save_calls, 0)

    def test_conflicting_adapter_never_fills_platform(self):
        config = Config(platform_id='', adapter='qq_official')
        self.assertEqual(discover_defaults(Context([Instance()]), config)['state'], 'conflict')
        self.assertEqual(config['platform_id'], '')
        self.assertEqual(config.save_calls, 0)

    def test_any_unknown_metadata_blocks_all_autofill(self):
        bad_instances = [object(), SimpleNamespace(meta=lambda: {}),
                         SimpleNamespace(meta=lambda: SimpleNamespace(id='qq')),
                         Instance(None), Instance(123), Instance(''),
                         Instance('qq', None), Instance('qq', ''),
                         Instance('qq', ' aiocqhttp')]
        for bad in bad_instances:
            with self.subTest(bad=type(bad).__name__):
                config = Config(platform_id='', adapter='')
                result = discover_defaults(Context([Instance(), bad]), config)
                self.assertEqual(result, {'state': 'invalid_metadata', 'candidate_ids': []})
                self.assertEqual(config, {'platform_id': '', 'adapter': ''})
                self.assertEqual(config.save_calls, 0)

    def test_duplicate_eligible_ids_never_become_one_candidate(self):
        instance = Instance()
        for instances in ([instance, instance], [Instance(), Instance()]):
            for manual in ('', 'qq-main'):
                config = Config(platform_id=manual, adapter='')
                result = discover_defaults(Context(instances), config)
                self.assertEqual(result['state'], 'invalid_metadata')
                self.assertEqual(config, {'platform_id': manual, 'adapter': ''})
                self.assertEqual(config.save_calls, 0)

    def test_lookup_must_return_the_exact_registered_instance(self):
        context = Context([Instance()])
        context.get_platform_inst = lambda value: Instance(value)
        config = Config(platform_id='qq-main', adapter='')
        self.assertEqual(discover_defaults(context, config)['state'], 'invalid_metadata')
        self.assertEqual(config['adapter'], '')

    def test_metadata_or_lookup_errors_never_echo_exception_text(self):
        def fail(*args):
            raise RuntimeError('token=private-secret config=private-settings')
        config = Config(platform_id='', adapter='')
        bad = SimpleNamespace(meta=fail)
        result = discover_defaults(Context([Instance(), bad]), config)
        self.assertEqual(result, {'state': 'invalid_metadata', 'candidate_ids': []})
        self.assertEqual(config.save_calls, 0)
        context = Context([Instance()])
        context.get_platform_inst = fail
        config['platform_id'] = 'qq-main'
        result = discover_defaults(context, config)
        self.assertEqual(result, {'state': 'unavailable', 'candidate_ids': ['qq-main']})
        self.assertEqual(config['adapter'], '')
        self.assertEqual(config.save_calls, 0)

    def test_metadata_changed_during_manual_lookup_is_not_saved(self):
        instance = Instance()
        context = Context([instance])
        def lookup(platform_id):
            instance.metadata.id = 'different-id'
            return instance
        context.get_platform_inst = lookup
        config = Config(platform_id='qq-main', adapter='')
        self.assertEqual(discover_defaults(context, config)['state'], 'invalid_metadata')
        self.assertEqual(config, {'platform_id': 'qq-main', 'adapter': ''})
        self.assertEqual(config.save_calls, 0)

    def test_unsupported_config_type_fails_soft(self):
        self.assertEqual(discover_defaults(Context([Instance()]), object()),
                         {'state': 'unavailable', 'candidate_ids': []})

    def test_wrong_adapter_manual_id_never_fills(self):
        config = Config(platform_id='discord', adapter='')
        context = Context([Instance('discord', 'discord'), Instance()])
        self.assertEqual(discover_defaults(context, config)['state'], 'not_found')
        self.assertEqual(config['adapter'], '')

    def test_unavailable_apis_fail_soft_without_raw_errors(self):
        def fail():
            raise RuntimeError('token=secret configuration=private')
        contexts = [object(), SimpleNamespace(platform_manager=object()),
                    SimpleNamespace(platform_manager=SimpleNamespace(get_insts=fail)),
                    SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: ())),
                    SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: None))]
        for context in contexts:
            config = Config(platform_id='', adapter='')
            self.assertEqual(discover_defaults(context, config),
                             {'state': 'unavailable', 'candidate_ids': []})
            self.assertEqual(config.save_calls, 0)

    def test_missing_save_api_never_changes_memory(self):
        config = {'platform_id': '', 'adapter': ''}
        original = dict(config)
        self.assertEqual(discover_defaults(Context([Instance()]), config)['state'], 'unavailable')
        self.assertEqual(config, original)

    def test_save_failure_rolls_back_missing_and_existing_keys(self):
        for initial in ({}, {'platform_id': '', 'adapter': ''},
                        {'platform_id': 'qq-main', 'adapter': ''},
                        {'adapter': 'aiocqhttp'}):
            config = Config(initial, mcp_token='private-secret', enabled=False)
            original = dict(config)
            config.fail_save = True
            result = discover_defaults(Context([Instance()]), config)
            self.assertEqual(result, {'state': 'save_failed', 'candidate_ids': ['qq-main']})
            self.assertEqual(config, original)
            self.assertEqual(config.save_calls, 1)
            self.assertNotIn('secret', str(result))

    def test_async_apis_are_not_run_or_reported_as_saved(self):
        async def async_value():
            raise AssertionError('must not run asynchronous discovery APIs')
        context = Context([Instance()])
        context.platform_manager.get_insts = async_value
        config = Config(platform_id='', adapter='')
        self.assertEqual(discover_defaults(context, config)['state'], 'unavailable')
        context = Context([Instance()])
        config.save_config = async_value
        self.assertEqual(discover_defaults(context, config)['state'], 'unavailable')
        self.assertEqual(config, {'platform_id': '', 'adapter': ''})
        config.save_config = lambda: async_value()
        self.assertEqual(discover_defaults(context, config)['state'], 'save_failed')
        self.assertEqual(config, {'platform_id': '', 'adapter': ''})

    def test_unexpected_save_result_rolls_back(self):
        config = Config(platform_id='', adapter='')
        config.save_config = lambda: False
        self.assertEqual(discover_defaults(Context([Instance()]), config)['state'], 'save_failed')
        self.assertEqual(config, {'platform_id': '', 'adapter': ''})

    def test_candidate_ids_are_bounded_without_deduplication_or_truncation(self):
        instances = [Instance('q' * MAX_ID_LENGTH)]
        instances += [Instance('qq-' + str(index)) for index in range(MAX_CANDIDATES + 20)]
        result = discover_defaults(Context(instances), Config())
        self.assertEqual(result['state'], 'multiple_candidates')
        self.assertEqual(len(result['candidate_ids']), MAX_CANDIDATES)
        self.assertEqual(result['candidate_ids'][0], 'q' * MAX_ID_LENGTH)
        self.assertTrue(all(len(value) <= MAX_ID_LENGTH for value in result['candidate_ids']))
        self.assertEqual(set(result), {'state', 'candidate_ids'})

    def test_unsafe_ids_fail_closed_and_are_never_echoed(self):
        for unsafe in ('qq\nsecret', 'qq\x1b[31m', 'qq secret', '<script>',
                       'https://host/?token=secret', 'q' * (MAX_ID_LENGTH + 1)):
            config = Config()
            result = discover_defaults(Context([Instance(unsafe)]), config)
            self.assertEqual(result, {'state': 'invalid_metadata', 'candidate_ids': []})
            self.assertEqual(config, {})

    def test_no_network_sensitive_defaults_or_automatic_enable(self):
        class GuardedConfig(Config):
            def get(self, key, *args):
                if key not in ('platform_id', 'adapter'):
                    raise AssertionError('must not read unrelated configuration')
                return super().get(key, *args)
        config = GuardedConfig(platform_id='', adapter='', enabled=False,
                               self_id='manual-bot', sender_id='manual-owner',
                               allowed_group_ids=['manual-group'], mcp_token='private-secret',
                               listen_host='127.0.0.1', listen_port=8769,
                               callback_hosts=['private-host'])
        before = dict(config)
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            result = discover_defaults(Context([Instance()]), config)
        self.assertEqual(result['state'], 'saved')
        self.assertEqual(config, dict(before, platform_id='qq-main', adapter='aiocqhttp'))
        self.assertNotIn('secret', str(result))
        self.assertNotIn('private-host', str(result))
        self.assertEqual(set(result), {'state', 'candidate_ids'})


if __name__ == '__main__':
    unittest.main()
