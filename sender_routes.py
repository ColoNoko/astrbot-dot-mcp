"""Validated sender routing. Routing never grants conversation permissions."""
from dataclasses import dataclass
import re


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class SenderRoute:
    sender_id: str
    tunnel_id: str
    listen_port: int


def parse_routes(config):
    items = config.get('sender_routes', [])
    if not isinstance(items, list) or len(items) > 16:
        raise RouteError('invalid_sender_routes')
    routes, senders, tunnels, ports = [], set(), set(), set()
    for item in items:
        if (not isinstance(item, dict) or
                set(item) - {'sender_id', 'tunnel_id', 'listen_port', '__template_key'} or
                ('__template_key' in item and item['__template_key'] != 'route')):
            raise RouteError('invalid_sender_routes')
        sender, tunnel, port = (item.get('sender_id'), item.get('tunnel_id'), item.get('listen_port'))
        if (type(sender) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', sender) or
                sender == config.get('self_id') or sender in senders or
                type(tunnel) is not str or not re.fullmatch(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', tunnel) or
                tunnel in tunnels or type(port) is not int or not 1024 <= port <= 65535 or port in ports):
            raise RouteError('invalid_sender_routes')
        routes.append(SenderRoute(sender, tunnel, port))
        senders.add(sender); tunnels.add(tunnel); ports.add(port)
    return tuple(routes)


def restrict_policy(policy, sender):
    """Intersection, including legacy all/selected modes; never widen an @ rule."""
    from .access_policy import AccessPolicy, GroupRule
    groups = tuple(GroupRule(rule.group_id, 'at' if rule.mode == 'at' else 'selected',
                            'selected', frozenset((sender,)))
                   for rule in policy.group_rules if rule.allows_sender(sender))
    return AccessPolicy(policy.binding, policy.private_sender_ids & {sender}, groups)
