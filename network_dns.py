"""One DNS choice with read-only compatibility for separate legacy settings.

The legacy marker never adds hostname disclosure to Google: callback and
attachment choices keep their old meanings until the administrator explicitly
selects a shared mode. This module selects modes only; each transport retains
its own target authorization, resolver lifetime and public-address checks.
"""

from .event_errors import EventError


LEGACY_CONFLICT_WARNING = 'network_dns_legacy_conflict'


def _network_mode(config):
    mode = config.get('network_dns', 'legacy')
    if type(mode) is not str or mode not in ('legacy', 'system', 'google'):
        raise EventError('invalid_network_dns')
    return mode


def _configured_mode(config, purpose, network_mode):
    if network_mode != 'legacy':
        return network_mode
    mode = config.get(purpose + '_dns', 'system')
    if type(mode) is not str or mode not in ('system', 'google'):
        raise EventError('invalid_' + purpose + '_dns')
    return mode


def effective_dns(config, purpose):
    """Return a fresh effective mode, failing closed on invalid configuration.

Callers must also use proxy_from_config to validate the full proxy settings.
Reading this again in an in-flight guard observes DNS selection or proxy
revocation without caching or modifying the source configuration.
    """
    if type(purpose) is not str or purpose not in ('callback', 'attachment'):
        raise EventError('invalid_network_dns_purpose')
    network_mode = _network_mode(config)
    mode = _configured_mode(config, purpose, network_mode)
    if mode == 'google' and config.get('proxy_enabled', False) is not True:
        prefix = purpose if network_mode == 'legacy' else 'network'
        raise EventError(prefix + '_dns_requires_proxy')
    return mode


def dns_settings(config):
    """Return safe display values without migrating or validating proxy access.

Matching legacy modes display their common choice, but the stored legacy
marker remains untouched. A conflict requires the fixed warning identified by
LEGACY_CONFLICT_WARNING; neither path is changed just by opening the page.
Use effective_dns for each purpose before enabling or saving runtime settings.
    """
    network_mode = _network_mode(config)
    callback = _configured_mode(config, 'callback', network_mode)
    attachment = _configured_mode(config, 'attachment', network_mode)
    conflict = callback != attachment
    return {'network_dns': 'legacy' if conflict else callback,
            'conflict': conflict,
            'callback_dns': callback, 'attachment_dns': attachment}
