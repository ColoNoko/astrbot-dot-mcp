"""Fail-closed conversation permissions, separate from the immutable bot owner.

A policy controls which incoming messages are captured. It grants no management
rights, arbitrary recipients, history joins, or independent MCP user identities.
"""
from dataclasses import dataclass
import re

MAX_SENDERS = 100
MAX_GROUPS = 100
QQ_ID = re.compile(r'[1-9][0-9]{0,19}\Z')


class PolicyError(ValueError):
    """Fixed, non-sensitive configuration errors."""


def _ids(value, error, *, maximum=MAX_SENDERS, forbidden=None):
    if (not isinstance(value, list) or len(value) > maximum or
            any(not isinstance(item, str) or QQ_ID.fullmatch(item) is None
                for item in value) or len(set(value)) != len(value) or
            (forbidden is not None and forbidden in value)):
        raise PolicyError(error)
    return frozenset(value)


@dataclass(frozen=True)
class GroupRule:
    group_id: str
    mode: str
    at_sender_scope: str
    sender_ids: frozenset

    def allows_sender(self, sender_id):
        return (self.mode == 'all' or
                (self.mode == 'at' and self.at_sender_scope == 'all') or
                sender_id in self.sender_ids)


@dataclass(frozen=True)
class AccessPolicy:
    binding: object
    private_sender_ids: frozenset
    group_rules: tuple

    @classmethod
    def from_config(cls, config, binding):
        version = config.get('access_rules_version', 0)
        if type(version) is not int or version not in (0, 1):
            raise PolicyError('invalid_access_rules_version')
        private_ids = _ids(config.get('private_sender_ids', []),
                           'invalid_private_sender_ids', forbidden=binding.self_id)
        if not private_ids and version == 0:
            private_ids = frozenset((binding.sender_id,))
        rules = config.get('group_rules', [])
        if not isinstance(rules, list) or len(rules) > MAX_GROUPS:
            raise PolicyError('invalid_group_rules')
        if not rules and version == 0:
            # Version 0 empty/missing fields preserve the complete old behavior.
            groups = _ids(config.get('group_ids', []), 'invalid_group_whitelist',
                          maximum=MAX_GROUPS)
            mode = config.get('group_trigger_mode', 'owner_at')
            if mode not in ('owner_at', 'all_messages'):
                raise PolicyError('invalid_group_trigger_mode')
            return cls(binding, private_ids, tuple(
                GroupRule(group, 'at' if mode == 'owner_at' else 'all',
                          'selected' if mode == 'owner_at' else 'all',
                          frozenset((binding.sender_id,)) if mode == 'owner_at' else frozenset())
                for group in sorted(groups)))
        parsed, seen = [], set()
        for rule in rules:
            if (not isinstance(rule, dict) or
                    set(rule) - {'group_id', 'mode', 'at_sender_scope', 'sender_ids', '__template_key'} or
                    ('__template_key' in rule and rule['__template_key'] != 'group')):
                raise PolicyError('invalid_group_rules')
            group, mode = rule.get('group_id'), rule.get('mode')
            scope = rule.get('at_sender_scope', 'selected')
            if (not isinstance(group, str) or QQ_ID.fullmatch(group) is None or
                    group in seen or mode not in ('at', 'selected', 'all') or
                    scope not in ('all', 'selected')):
                raise PolicyError('invalid_group_rules')
            senders = _ids(rule.get('sender_ids', []), 'invalid_group_rules',
                           forbidden=binding.self_id)
            parsed.append(GroupRule(group, mode, scope, senders))
            seen.add(group)
        # Never merge explicit rules with legacy groups or silently skip errors.
        return cls(binding, private_ids, tuple(parsed))

    def group_rule(self, group_id):
        return next((rule for rule in self.group_rules if rule.group_id == group_id), None)

    def authorizes_stored(self, row):
        """Check current membership only; a persisted row cannot prove a raw @.

        Live captures and reactive sends independently validate the raw top-level
        bot mention. Stored reads remain possible after a restart without a live
        event handle, but removed conversations/senders are immediately revoked.
        """
        try:
            kind, group, sender = (row['conversation_type'], row['group_id'], row['sender_id'])
        except (KeyError, IndexError, TypeError):
            return False
        if (not isinstance(sender, str) or QQ_ID.fullmatch(sender) is None or
                sender == self.binding.self_id or not isinstance(group, str)):
            return False
        if kind == 'private':
            return group == '' and sender in self.private_sender_ids
        if kind == 'group':
            rule = self.group_rule(group)
            return rule is not None and rule.allows_sender(sender)
        return False
