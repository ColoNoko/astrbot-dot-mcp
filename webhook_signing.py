"""Standard Webhooks signing adapted from wechat-dot-bridge (MIT, cd233).

Copyright (c) 2026 cd233. See THIRD_PARTY_EVENTS_LICENSE.txt.
"""

import base64
from datetime import datetime, timezone

from standardwebhooks import Webhook

from .event_errors import EventError


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise EventError("invalid_signing_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (ValueError, TypeError):
        raise EventError("invalid_signing_secret") from None
    if not 24 <= len(key) <= 64:
        raise EventError("invalid_signing_secret")
    return key


def signed_headers(event_id, body, secret, signed_at, subscription_id, old_secret=None):
    """Sign the exact bytes transmitted, once for each active rotation key."""
    try:
        payload = body.decode("utf-8")
        event_id.encode("ascii")
        when = datetime.fromtimestamp(int(signed_at), timezone.utc)
        signatures = [Webhook(signing_key(key)).sign(event_id, when, payload)
                      for key in (secret, old_secret) if key]
    except EventError:
        raise
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise EventError("invalid_signing_payload") from None
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": str(int(signed_at)),
            "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}

