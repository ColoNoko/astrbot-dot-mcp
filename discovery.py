"""Conservative, local-only platform defaults for AstrBot 4.28.2.

Only an absent key or the exact empty string is a defaultable value. Discovery
never reads adapter clients, account IDs, connection settings, or credentials.
"""
import inspect
import re


ADAPTER = 'aiocqhttp'
MAX_CANDIDATES = 16
MAX_ID_LENGTH = 128
def valid_platform_id(value):
    """Metadata IDs are user-defined Unicode names, not UUIDs or path fragments.

    Keep the exact string (no normalization/case-folding); reject controls,
    invisible separators, surrogates and padded values before display/routing.
    """
    return (type(value) is str and 1 <= len(value) <= MAX_ID_LENGTH and
            value.isprintable() and not any(c.isspace() for c in value) and
            all(ord(c) >= 128 or c.isalnum() or c in '_.:-' for c in value))

_MISSING = object()


def _status(state, candidates=()):
    # IDs are complete, allowlisted values, never a truncated or escaped config.
    return {'state': state, 'candidate_ids': [value for value in candidates
            if valid_platform_id(value)][:MAX_CANDIDATES]}


def _blank(value):
    return value is _MISSING or (type(value) is str and value == '')


def _call_sync(function, *args):
    if not callable(function) or inspect.iscoroutinefunction(function):
        raise ValueError('unsupported_sync_api')
    result = function(*args)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise ValueError('unsupported_sync_api')
    return result


def _metadata(instance):
    meta = _call_sync(getattr(instance, 'meta', None))
    name, platform_id = meta.name, meta.id
    if (type(name) is not str or not name or name != name.strip() or
            len(name) > 128 or not name.isprintable() or
            not valid_platform_id(platform_id)):
        raise ValueError('invalid_platform_metadata')
    return name, platform_id


def discover_defaults(context, config):
    """Fill only unambiguous blank platform_id/adapter fields, then save once.

    Returns only ``state`` and at most 16 safe instance IDs (128 characters each).
    ``saved`` means the synchronous save_config call returned normally. On any
    save failure, the original in-memory fields are restored; a partial disk
    write cannot be ruled out, and no successful persistence is reported.
    """
    if not isinstance(config, dict):
        return _status('unavailable')
    original = {key: dict.get(config, key, _MISSING)
                for key in ('platform_id', 'adapter')}
    platform_id, adapter = original['platform_id'], original['adapter']
    if not _blank(adapter) and (type(adapter) is not str or adapter != ADAPTER):
        return _status('conflict')
    if not _blank(platform_id):
        if not valid_platform_id(platform_id):
            return _status('conflict')
        if not _blank(adapter):
            return _status('unchanged')

    try:
        instances = _call_sync(context.platform_manager.get_insts)
        if type(instances) is not list:
            return _status('unavailable')
    except Exception:
        return _status('unavailable')

    eligible = []
    seen = set()
    try:
        for instance in instances:
            name, instance_id = _metadata(instance)
            if name == ADAPTER:
                # Duplicate IDs are ambiguous even when both entries are the
                # same object. Never deduplicate them into one candidate.
                if instance_id in seen:
                    return _status('invalid_metadata')
                seen.add(instance_id)
                eligible.append((instance_id, instance))
    except Exception:
        return _status('invalid_metadata')
    candidates = [item[0] for item in eligible]

    if _blank(platform_id):
        if not eligible:
            return _status('no_candidates')
        if len(eligible) != 1:
            return _status('multiple_candidates', candidates)
        # One loaded instance is insufficient if another configured, enabled
        # adapter failed to load. Read only routing metadata from the public API.
        try:
            root_config = _call_sync(context.get_config)
            configured = root_config.get('platform') if isinstance(root_config, dict) else None
            if not isinstance(configured, list):
                return _status('unavailable', candidates)
            enabled_ids = []
            for entry in configured:
                if not isinstance(entry, dict) or not isinstance(entry.get('type'), str):
                    return _status('invalid_metadata', candidates)
                if entry['type'] == ADAPTER and entry.get('enable') is True:
                    configured_id = entry.get('id')
                    if not valid_platform_id(configured_id):
                        return _status('invalid_metadata', candidates)
                    enabled_ids.append(configured_id)
            if enabled_ids != [eligible[0][0]]:
                return _status('configured_ambiguity', candidates)
        except Exception:
            return _status('unavailable', candidates)
        selected_id = eligible[0][0]
    else:
        try:
            selected = _call_sync(context.get_platform_inst, platform_id)
        except Exception:
            return _status('unavailable', candidates)
        matches = [instance for candidate_id, instance in eligible
                   if candidate_id == platform_id]
        if not matches or selected is None:
            return _status('not_found', candidates)
        if selected is not matches[0]:
            return _status('invalid_metadata')
        try:
            if _metadata(selected) != (ADAPTER, platform_id):
                return _status('invalid_metadata')
        except Exception:
            return _status('invalid_metadata')
        selected_id = platform_id

    try:
        save = getattr(config, 'save_config', None)
        if not callable(save) or inspect.iscoroutinefunction(save):
            return _status('unavailable', candidates)
        updates = {'platform_id': selected_id, 'adapter': ADAPTER}
        for key, value in updates.items():
            if _blank(original[key]):
                dict.__setitem__(config, key, value)
        # AstrBot 4.28.2's synchronous save_config returns None on success.
        if _call_sync(save) is not None:
            raise ValueError('unsupported_save_result')
    except Exception:
        for key, value in original.items():
            if value is _MISSING:
                dict.pop(config, key, None)
            else:
                dict.__setitem__(config, key, value)
        return _status('save_failed', candidates)
    return _status('saved', candidates)
