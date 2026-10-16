"""Offline shared DNS migration, disclosure boundary and revocation regressions."""

import copy
import importlib
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
if 'astrbot_dot_plugin' not in sys.modules:
    package = types.ModuleType('astrbot_dot_plugin')
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
module = importlib.import_module('astrbot_dot_plugin.network_dns')
EventError = module.EventError


class NetworkDNSConfigTests(unittest.TestCase):
    def modes(self, config):
        return tuple(module.effective_dns(config, purpose)
                     for purpose in ('callback', 'attachment'))

    def test_absent_or_default_legacy_marker_uses_system_without_mutation(self):
        for config in ({}, {'network_dns': 'legacy'}):
            original = copy.deepcopy(config)
            with self.subTest(config=config):
                self.assertEqual(self.modes(config), ('system', 'system'))
                self.assertEqual(module.dns_settings(config), {
                    'network_dns': 'system', 'conflict': False,
                    'callback_dns': 'system', 'attachment_dns': 'system'})
                self.assertEqual(config, original)

    def test_matching_legacy_modes_display_common_mode_without_migration(self):
        for mode in ('system', 'google'):
            for marker in ({}, {'network_dns': 'legacy'}):
                config = dict(marker, callback_dns=mode, attachment_dns=mode,
                              proxy_enabled=True)
                original = copy.deepcopy(config)
                with self.subTest(mode=mode, marker=marker):
                    self.assertEqual(self.modes(config), (mode, mode))
                    self.assertEqual(module.dns_settings(config), {
                        'network_dns': mode, 'conflict': False,
                        'callback_dns': mode, 'attachment_dns': mode})
                    self.assertEqual(config, original)

    def test_conflicts_preserve_both_directions_and_report_fixed_warning(self):
        self.assertEqual(module.LEGACY_CONFLICT_WARNING, 'network_dns_legacy_conflict')
        for callback, attachment in (('system', 'google'), ('google', 'system')):
            for marker in ({}, {'network_dns': 'legacy'}):
                config = dict(marker, callback_dns=callback,
                              attachment_dns=attachment, proxy_enabled=True)
                original = copy.deepcopy(config)
                with self.subTest(callback=callback, marker=marker):
                    self.assertEqual(self.modes(config), (callback, attachment))
                    self.assertEqual(module.dns_settings(config), {
                        'network_dns': 'legacy', 'conflict': True,
                        'callback_dns': callback, 'attachment_dns': attachment})
                    self.assertEqual(config, original)

    def test_missing_legacy_value_defaults_independently_not_to_other_choice(self):
        for purpose in ('callback', 'attachment'):
            config = {purpose + '_dns': 'google', 'proxy_enabled': True}
            expected = ('google', 'system') if purpose == 'callback' else ('system', 'google')
            with self.subTest(purpose=purpose):
                self.assertEqual(self.modes(config), expected)
                self.assertTrue(module.dns_settings(config)['conflict'])

    def test_explicit_shared_choice_governs_both_without_rewriting_legacy_or_secrets(self):
        for mode in ('system', 'google'):
            config = {'network_dns': mode, 'callback_dns': 'google',
                      'attachment_dns': 'system', 'proxy_enabled': True,
                      'proxy_password': 'SECRET_PROXY',
                      'callback_hosts': ['callback.example.com'],
                      'mcp_token': 'SECRET_TOKEN'}
            original = copy.deepcopy(config)
            with self.subTest(mode=mode):
                self.assertEqual(self.modes(config), (mode, mode))
                status = module.dns_settings(config)
                self.assertEqual(status, {'network_dns': mode, 'conflict': False,
                    'callback_dns': mode, 'attachment_dns': mode})
                self.assertNotIn('SECRET', repr(status))
                self.assertEqual(config, original)

    def test_explicit_shared_choice_ignores_dormant_invalid_legacy_values(self):
        for mode in ('system', 'google'):
            config = {'network_dns': mode, 'callback_dns': 'SECRET_INVALID',
                      'attachment_dns': {'secret': 'SECRET'}, 'proxy_enabled': True}
            self.assertEqual(self.modes(config), (mode, mode))
            self.assertEqual(module.dns_settings(config)['network_dns'], mode)
            self.assertNotIn('SECRET', repr(module.dns_settings(config)))

    def test_invalid_shared_values_fail_closed_with_fixed_error(self):
        for mode in (None, '', False, 1, [], {}, ['google'], 'Google',
                     'system ', 'https://dns.google/SECRET'):
            config = {'network_dns': mode, 'callback_dns': 'google',
                      'attachment_dns': 'system', 'proxy_enabled': True}
            for operation in (self.modes, module.dns_settings):
                with self.subTest(mode=mode), self.assertRaisesRegex(
                        EventError, '^invalid_network_dns$'):
                    operation(config)

    def test_invalid_active_legacy_values_fail_closed_per_purpose(self):
        for purpose in ('callback', 'attachment'):
            for mode in (None, '', False, 1, [], {}, ['google'], 'Google', 'SECRET'):
                config = {purpose + '_dns': mode, 'proxy_enabled': True}
                with self.subTest(purpose=purpose, mode=mode):
                    with self.assertRaisesRegex(EventError, '^invalid_' + purpose + '_dns$'):
                        module.effective_dns(config, purpose)
                    with self.assertRaisesRegex(EventError, '^invalid_' + purpose + '_dns$'):
                        module.dns_settings(config)

    def test_invalid_purpose_never_selects_another_config_key(self):
        for purpose in (None, '', False, 1, [], {}, 'network', 'callback_dns', 'SECRET'):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(
                    EventError, '^invalid_network_dns_purpose$'):
                module.effective_dns({'network_dns': 'system'}, purpose)

    def test_google_requires_exact_enabled_proxy_without_silent_fallback(self):
        for purpose in ('callback', 'attachment'):
            for proxy_enabled in (None, False, 0, 1, '', 'true', [], {}):
                for marker, prefix in (({'network_dns': 'google'}, 'network'),
                                       ({purpose + '_dns': 'google'}, purpose)):
                    config = dict(marker, proxy_enabled=proxy_enabled)
                    with self.subTest(purpose=purpose, config=config):
                        with self.assertRaisesRegex(EventError, '^' + prefix + '_dns_requires_proxy$'):
                            module.effective_dns(config, purpose)
                        self.assertEqual(module.dns_settings(config)[purpose + '_dns'], 'google')
            with self.assertRaisesRegex(EventError, '^network_dns_requires_proxy$'):
                module.effective_dns({'network_dns': 'google'}, purpose)

    def test_revocation_reads_current_mode_and_proxy_each_time(self):
        config = {'network_dns': 'google', 'proxy_enabled': True,
                  'callback_dns': 'system', 'attachment_dns': 'google'}
        captured = self.modes(config)
        config['network_dns'] = 'system'
        self.assertEqual(self.modes(config), ('system', 'system'))
        self.assertNotEqual(self.modes(config), captured)
        config['network_dns'] = 'legacy'
        self.assertEqual(self.modes(config), ('system', 'google'))
        config['attachment_dns'] = 'system'
        self.assertEqual(self.modes(config), ('system', 'system'))
        config['network_dns'] = 'google'
        config['proxy_enabled'] = False
        for purpose in ('callback', 'attachment'):
            with self.assertRaisesRegex(EventError, '^network_dns_requires_proxy$'):
                module.effective_dns(config, purpose)
        config['network_dns'] = 'SECRET_INVALID'
        with self.assertRaisesRegex(EventError, '^invalid_network_dns$'):
            self.modes(config)

    def test_dns_selection_does_not_read_targets_credentials_or_attachment_data(self):
        class ObservedConfig(dict):
            def get(self, key, default=None):
                if key not in ('network_dns', 'callback_dns', 'attachment_dns', 'proxy_enabled'):
                    raise AssertionError('DNS selection read unrelated data')
                return super().get(key, default)
        config = ObservedConfig(network_dns='google', proxy_enabled=True,
            callback_hosts=['SECRET_CALLBACK'], attachment_url='SECRET_URL',
            proxy_username='SECRET_USER', proxy_password='SECRET_PASSWORD')
        self.assertEqual(self.modes(config), ('google', 'google'))
        self.assertEqual(module.dns_settings(config)['network_dns'], 'google')


if __name__ == '__main__':
    unittest.main()
