# Public source backup

This directory contains the v0.3.2 plugin source, documentation, synthetic tests,
icons, and the bundled official Linux amd64 Tunnel runtime with its original
license, checksum, SPDX, manifest, and provenance materials.

The plugin identity `astrbot_plugin_dot_private` is intentionally preserved for
in-place upgrade compatibility. Public repository visibility does not change
the plugin's private-message handling or authentication requirements.

## Included release

`dist/astrbot_plugin_dot_private_v0.3.2.zip` is regenerated from this sanitized
source tree. The archive contains one `astrbot_plugin_dot_private/` directory
and excludes `dist`, runtime data, local configuration, test logs, caches,
virtual environments, and private working history. Its SHA-256 is in
`dist/SHA256SUMS.txt`.

All account identifiers, platform names, credentials, and messages in the test
fixtures are synthetic examples. Never use fixture credentials in deployment.
Installation paths in documentation are generic examples. Real configuration
and message databases must remain outside this public backup.

## Third-party notices

- The official Tunnel runtime and bundled dependencies retain their original
  licenses and notices under `vendor/linux-amd64/`. The existing manifest
  explicitly states that its Sigstore signature was not verified.
- MCP Events-related third-party notice: `THIRD_PARTY_EVENTS_LICENSE.txt`.
- `logo.png` and `pages/connection/assets/logo.png` are supplied character
  artwork, retained unchanged. Copyright belongs to the respective rights
  holders. No ownership or redistribution license for the artwork is claimed
  or granted by this backup.

This backup does not add a license for the original plugin code.

## Backup verification

The sanitized source was rechecked on 2026-10-14 with Python 3.12.14 and
aiohttp 3.11.18: 224 Python tests discovered, 211 passed, 13 skipped (12 optional
real-browser cases and one optional SDK type-validation case). The 26 Node DOM
and bridge tests passed. This does not establish live AstrBot, QQ, or Tunnel
connectivity; see `TEST_REPORT.md` for the original verification boundaries.
