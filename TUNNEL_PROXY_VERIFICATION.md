# Tunnel proxy verification (v0.3.2)

Verified against the official `openai/tunnel-client` **v0.0.16** tag on
2026-10-14, resolving to commit
`5f99daabd4aa4a77049e6d81d54a0d8c18335397`. This work does not change, download,
upgrade, or execute the packaged Tunnel binary.

## Official contract and implementation choice

- [Configuration reference](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md#outbound-http-proxy): the supported target-specific setting is
  `CONTROL_PLANE_HTTP_PROXY` / `control_plane.http_proxy`. An explicit target
  proxy overrides environment proxy selection, including `NO_PROXY`.
- [Proxy parser](https://github.com/openai/tunnel-client/blob/v0.0.16/pkg/runtimeconfig/proxy.go): proxy values accept a literal HTTP(S) URL or an `env:` reference.
  The parser does not implement a `file:` secret reference for proxy values.
- [Configuration schema](https://github.com/openai/tunnel-client/blob/v0.0.16/pkg/runtimeconfig/file_config.go): the YAML field is `http_proxy`, not `proxy_url`.
- [Runtime configuration](https://github.com/openai/tunnel-client/blob/v0.0.16/pkg/runtimeconfig/config.go): control-plane and MCP proxy resolution are separate; the
  control-plane override is not inherited by MCP channels.
- [Proxy transport](https://github.com/openai/tunnel-client/blob/v0.0.16/pkg/transport/proxy.go): an explicitly selected proxy installs a fixed proxy function on a cloned
  transport; it has no connection-failure fallback to a direct route.
- [Go HTTP transport](https://pkg.go.dev/net/http#Transport): HTTP proxies support HTTPS via CONNECT; URL userinfo supplies proxy authentication. The plugin limits
  configuration to `http://host:port` and optional separate Basic credentials.

The supervisor therefore supplies `CONTROL_PLANE_HTTP_PROXY` only in the new
child's allowlisted environment. It does not set global, MCP, or ordinary
HTTP(S) proxy variables. The setting is absent when disabled. Parent and other
AstrBot process environments are unchanged. Proxy credentials are percent-encoded
into that child-only URL, never put into command-line arguments, generated
runtime YAML, public status, or plugin logs. As with all child environment
secrets, privileged host users may still inspect process environment memory.

The configured MCP endpoint remains restricted to literal IPv4/IPv6 loopback.
Its transport has no selected proxy, and no ambient proxy reaches the child.
`NO_PROXY=localhost,127.0.0.1,::1` is also set, but correctness does not rely on
`NO_PROXY` overriding an explicit global proxy. Supervisor health requests use
`trust_env=False`, `proxy=None`, and disabled redirects.

## Test scope

`python -m unittest discover -s tests -p test_tunnel_runtime.py -v` passes
30 tests, including 9 added proxy tests. Tests use synthetic credentials,
synthetic ELF bytes, mocked process creation, and loopback-only health fixtures.

Coverage: enable/disable/reload, child-only environment, unchanged parent
environment, uppercase/lowercase ambient proxy exclusion, IPv4/IPv6 MCP bypass
configuration, direct health probes, invalid settings, redaction, fixed startup
failure/exit/timeout codes, and no direct retry or replacement child on failure.

No live Tunnel process, external proxy, real credentials, control-plane API,
QQ connection, or end-to-end CONNECT test was used. Binary stderr remains
discarded; failures therefore report existing fixed codes such as
`start_failed`, `process_exited`, `startup_timeout`, or `not_ready`, rather than
claiming that a particular proxy returned 407. Local health is not proof of
proxy connectivity: `connected` remains null and `roundtrip_verified` false.
