> 当前版本为 v0.5.6；分路配置、最新行为与限制见 [版本说明](UPDATE_0_5_6.md)、[README](README.md)。下文含历史版本记录，旧选项可能已退役；当前 Events 使用 aiohttp，消息读取不自动附带历史。

# HTTP 代理边界

本版只支持可选 HTTP forward proxy，目标 HTTPS 流量通过 CONNECT。默认关闭，不读取用户机器环境代理，不修改 os.environ，也不提供 SOCKS/HTTPS-proxy 承诺。

- Tunnel：官方 v0.0.16 的 CONTROL_PLANE_HTTP_PROXY，仅注入受管子进程环境。没有为 MCP 目标设置代理；MCP loopback 与本地健康检查保持直连。该显式代理配置不受 NO_PROXY 改成直连。
- Events：challenge 和实际签名投递使用同一配置。先检查允许域名、HTTPS/443 和本地解析的所有 IP 都是公网地址；CONNECT 目标使用选中的数字 IP，TLS server_hostname 和 HTTPS Host 保持原域名。拒绝重定向、私网回调、无效证书；失败不尝试直接连接。
- 配置里的代理主机可以是用户明确指定的内网地址，这是转发服务器，不是允许私网作为回调目标。代理网络行为仍需信任：客户端不能替你控制代理内部如何转发。
- 默认系统模式下，本地 DNS 必须能正确解析回调域名。v0.4.3 可明确选择 Google 加密 DNS 经代理解析（见 CALLBACK_DNS.md）。此实现没有“由代理远程解析目标域名”模式，避免解除 DNS 地址检查。代理不支持数字 IP CONNECT 时会失败。
- 代理用户名/密码只写不回显，存储在用户本机 AstrBot 配置中，secret 只是 UI 遮罩，不是磁盘加密；Tunnel 运行时子进程环境也包含认证代理 URL，有权限查看进程的本机管理员可读取。请保护配置/备份。
- HTTP 代理到客户端这一跳不加密，Basic 代理认证本身不受目标 TLS 保护；只连接你信任的代理和网络。HTTPS 目标内容仍由原域名证书验证的 TLS 加密，不关闭验证。
- 代理服务器在宿主机时，不要填容器的 127.0.0.1；使用容器实际可达地址。插件不会自动修改容器网络、NAS 防火墙或代理监听设置。

测试只使用合成凭据和云端 loopback fake CONNECT/TLS 服务，不连接任何用户或公网代理。官方 Tunnel 二进制未执行，真实网络连通及用户代理配置仍需实际验收。

源码依据：[官方 pinned Tunnel proxy 配置](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md)、[aiohttp 3.11.18 CONNECT/TLS 实现](https://github.com/aio-libs/aiohttp/blob/v3.11.18/aiohttp/connector.py)。


v0.4.0 的附件HTTPS下载也使用相同显式代理设置与公共地址固定，不使用系统代理或失败直连。实际代理可能对数字CONNECT目的地与域名规则采取不同路径；本版没有隐式开启“信任代理DNS”模式。


可选“微信兼容（HTTPCore）”只更换 Events HTTP 实现，仍固定已核验公网IP并验证原主机TLS，不改变代理或DNS。两个后端均不承诺适配所有规则型代理。没有自动失败回退；更换后端需用户在管理页保存/应用。
