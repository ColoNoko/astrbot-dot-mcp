# Connection page contract

## Host integration

Target: AstrBot v4.28.2 native plugin pages. Entry: `pages/connection/index.html`.
The host injects `window.AstrBotPluginPage`; the page waits for `ready()` and uses
only `apiGet('status')`, `apiPost('settings', body)`, `apiPost('action', body)`,
and the read-only query `apiPost('history', body)`.
The official bridge already unwraps a `{status: 'ok', data: value}` response and
rejects an error envelope; handlers return plain business JSON. The page must not
apply a second `.data` unwrap. No direct Dashboard fetch or parent DOM access.

Current online documentation uses newer “View” names. Do not rename the directory
or bridge from this pinned Page integration without checking the targeted release.
The shell title is supplied by `pages.connection.title` in plugin i18n resources.

### In-document tabs

The v4.28.2 sidebar opens the first native plugin page; its page shell has no
sibling-page selector and its bridge has no navigation operation. Therefore
`pages/connection/index.html` is the single entry with visible **连接设置** and
**消息记录** tabs. `tabs.js` changes only local panel visibility and accessible
tab state. Arrow keys, Home and End move focus between the two tabs. It never
navigates the parent, adds an iframe, or accesses another page's assets.

The connection panel stays mounted, preserving unsaved fields and masked secret
inputs. `history.js` exports a reusable controller scoped to `#history-panel`,
with `history-` IDs, `h-` classes, and panel-local busy state. `history.css` uses
scoped element rules and namespaced classes. History initializes once, when the
user first opens that tab; repeated tab changes do not repeat queries or writes.
History errors do not disable or modify connection settings.

### Native sender routes

`settings.sender_routes` is an optional, read-only list of up to 16 native plugin
route entries (`sender_id`, `tunnel_id`, `listen_port`). This page never edits or
posts it and excludes it from editable-form dirty comparison. Configured routes
allow a blank global Tunnel ID; the shared Tunnel key is still required when
Tunnel is enabled. Multi-route mode ignores the global Tunnel ID and listen port.

`runtime.routes` is an optional list of active child statuses: `sender_id`,
`listen_port`, `mcp_ready`, `error_code`, and the usual `tunnel` status object.
The page renders per-sender local status and aggregate counts without claiming
remote connectivity. With configured or active routes, the standalone reconnect
button is disabled; Apply restarts all routes. `multi_route_apply_required` and
`invalid_sender_routes` render fixed instructions to use Apply or native settings.

### Read-only message history

The history controller obtains `csrf_token` from `GET status`, then queries:

- `POST history`: `{csrf_token, action: "conversations", before: null, limit: 50}`
- `POST history`: `{csrf_token, action: "messages", conversation_type: "private" | "group", group_id, before: null, limit: 30}`

`group_id` is empty for private messages and comes from a returned conversation
for groups. Both responses expose `next_before` as an opaque string or `null`;
subsequent pages pass that value back in `before`. There is no overall 50-group
display cap. Messages are validated against the selected conversation before
rendering, and new conversations do not replace the currently visible messages.

History only displays bounded, plain-text message/reply fields and safe attachment
placeholders. No attachment URLs, internal routing/source IDs, raw errors, or
credentials enter the DOM. Group views display a group-privacy reminder. Processing
states are separate from reply states: `adapter_completed` means the adapter
returned, not that QQ delivery was confirmed; `uncertain` never triggers a retry.
No send, complete, or delete controls are provided. Storage usage and the `blocked`
flag come from the backend. This UI neither enforces nor changes storage retention.
The `multi_route_history_unavailable` error explains that this Dashboard history
view is unavailable in multi-route mode; each route's MCP history tools remain
available. It never merges records across routes or falls back to a single route.

All scripts, CSS and images are local relative assets. `assets/logo.png` is an
unchanged byte-for-byte copy of the user-provided root `logo.png`. No CDN, framework,
build step, inline JavaScript, browser credential storage or production mock bridge.

## Status payload

GET and successful POST responses share this shape:

```json
{
  "settings": {
    "enabled": false,
    "platform_id": "",
    "adapter": "",
    "self_id": "",
    "listen_host": "127.0.0.1",
    "listen_port": 8769,
    "callback_hosts": [],
    "tunnel_id": "",
    "tunnel_enabled": false,
    "proxy_enabled": false,
    "proxy_url": ""
  },
  "secrets": {
    "mcp_token_set": false,
    "tunnel_key_set": false,
    "proxy_username_set": false,
    "proxy_password_set": false
  },
  "platforms": [{"id": "qq-main", "adapter": "aiocqhttp"}],
  "runtime": {
    "mcp_ready": false,
    "error_code": null,
    "tunnel": {
      "enabled": false, "state": "disabled", "process_running": false,
      "healthy": false, "ready": false, "binary_available": false,
      "connected": null, "roundtrip_verified": false, "last_error_code": null
    }
  },
  "versions": {"plugin": "0.3.2"},
  "csrf_token": "opaque-per-session-token",
  "revision": "opaque-current-config-revision"
}
```

The page uses explicitly selected fields and bounded text rendering. It never
renders raw errors or JSON. `runtime.error_code` and `tunnel.last_error_code`
map through fixed Chinese messages; `tunnel.error_code` is also accepted. Proxy
validation uses `invalid_proxy_settings`; URL changes that retain old credentials
use `proxy_auth_reentry_required`. Local health/ready flags never establish remote
connectivity, QQ delivery, or message roundtrip success.

No secret values are returned, including proxy usernames. Only the four boolean
presence flags above are exposed. A stored invalid proxy URL is blanked by the
backend, with `runtime.error_code: "invalid_proxy_settings"`; the page still allows
repairing that configuration. An unsafe nonempty URL in a response fails closed
and is never placed into the form.

Platform identity is preserved exactly, without trim, normalization or truncation.
Valid IDs contain 1–128 Unicode code points; ASCII is limited to letters, digits,
`_ . : -`, with Unicode allowed except control, format, separator and whitespace
characters. Only exact `aiocqhttp` adapter instances are offered.

## Save and actions

`POST settings`:

```json
{
  "csrf_token": "opaque-per-session-token",
  "revision": "opaque-current-config-revision",
  "settings": {
    "platform_id": "qq-main",
    "adapter": "aiocqhttp",
    "self_id": "123456789",
    "listen_host": "127.0.0.1",
    "listen_port": 8769,
    "callback_hosts": ["callback.example.com"],
    "tunnel_enabled": false,
    "tunnel_id": "",
    "proxy_enabled": false,
    "proxy_url": ""
  },
  "clear_proxy_auth": false
}
```

`settings` contains the ten advanced fields above, excluding basic `enabled`.
The only optional top-level replacements are `tunnel_key`, `proxy_username`, and
`proxy_password`. Inputs start blank and are never populated from status. Blank
values are omitted to preserve stored values; nonempty values replace them.
Successful save clears replacement inputs and the clear-auth checkbox, stops the
saved runtime, and asks the user to apply explicitly. Backend validation is
always authoritative.

The page permits staging blank platform/QQ values; nonempty platform IDs must be
in the exact `aiocqhttp` list. Advanced saves allow only loopback, ports 1024–65535,
and up to eight unique lowercase exact callback hostnames.

`POST action`: `{csrf_token, revision, action}` with only `apply` or `stop`.
Applying is blocked while edits are unsaved. Stopping remains available and
preserves unsaved input. No request changes basic `enabled`; save/apply/stop are
distinct. The backend creates and persists missing local MCP Bearer authentication
on explicit enabled startup/apply. Existing valid authentication is preserved.
The integrated Tunnel reads it server-side. There is no manual token input,
`mcp_token` save payload, token-generation action, or generation confirmation.
GET status, page load and refresh never generate or save credentials.

### Proxy settings

- Default: `proxy_enabled: false`, `proxy_url: ""`.
- URL: ASCII, at most 1024 characters, exact `http://` prefix, explicit port 1–65535.
  An IPv4 address, bracketed IPv6 address or DNS hostname is allowed. No URL
  username/password, query, fragment, percent-encoded authority, or path other
  than an optional trailing `/`. Unspecified and multicast IPs are invalid.
- HTTPS destinations use HTTP CONNECT. SOCKS and HTTPS proxy URLs are unsupported.
- Username: optional ASCII, at most 256 characters, no colon or control characters.
  Password: optional ASCII, at most 4096 characters, no control characters.
  A password requires a supplied or already stored username.
- `clear_proxy_auth: true` explicitly removes both stored credential fields.
  Clearing disables the two credential inputs; the page omits any typed
  replacements from that request. Clear plus nonempty replacements is rejected
  by the backend. Switching proxy off alone retains its URL and credentials.
- A changed proxy URL with saved credentials requires re-entry of every already
  stored credential field, or explicit clearing. The backend enforces this too.
  Credentials are never silently carried to another proxy endpoint.
- The proxy applies to Tunnel outbound connections and event callbacks. Local
  MCP and local health checks bypass it. Proxy failure never falls back to a
  direct outbound connection. Existing callback public-address/DNS checks still
  run locally; this is not a remote-DNS or SSRF-bypass option.
- In a NAS/Docker deployment, `127.0.0.1` refers to the AstrBot container itself.
  For a proxy elsewhere, use a host/LAN address reachable from that container.

Requests are serialized through disabled controls. Responses refresh CSRF/revision.
A failed mutation is never retried automatically; one read-only status refresh
reconciles state while preserving typed values and explicit clear intent. Dirty
refresh detects newer revisions and disables saving until explicit reload/discard.
A failed status read disables mutations until a verified refresh. No bridge means
all mutations remain disabled.

## Verification

- `node --check pages/connection/app.js`
- `node --check pages/connection/history.js`
- `node --check pages/connection/tabs.js`
- `node --test tests/test_plugin_page_logic.cjs`: dependency-free synthetic DOM and
  bridge unit checks, not a rendered browser or real AstrBot integration.
- `node --test tests/test_history_page.mjs`: dependency-free history and integrated
  tab checks, including hostile text, paging, conversation isolation, dirty settings
  preservation, keyboard navigation, and interrupted/repeated tab flows.
- `python -m unittest discover -s tests -p test_plugin_page_browser.py -v`:
  optional Chromium rendering/interactions against a localhost synthetic bridge.
  Do not retry browser execution where the environment has already denied it.
  The suite is skipped when Chromium/Playwright is missing or required process
  sockets are denied. An optional `DOT_PAGE_SCREENSHOTS` output directory saves
  desktop/mobile light/dark PNGs.

Neither suite uses real credentials, connects to real MCP/Tunnel, or validates QQ.
