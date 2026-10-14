# 内置 Tunnel 运行边界

此模块只管理同一 AstrBot 容器内的官方 `tunnel-client-runtime` 子进程。默认关闭；导入插件、打开页面和安装 ZIP 不会启动客户端、下载软件、创建 Tunnel 或生成凭证。启用后的网络连接使用用户自己提供的 Tunnel ID 与运行密钥。

## 本地连接与密钥

- MCP 目标由插件代码提供，限定为带明确端口的 `http://127.0.0.1:<port>/mcp` 或 `http://[::1]:<port>/mcp`。没有任意 URL、命令、执行文件路径或控制面地址的页面输入。
- 运行密钥文件限定在本插件的 `plugin_data` 目录内，必须属于运行用户、权限 `0600`、非符号链接且无硬链接。用户持久密钥文件由配置层管理；停止 Tunnel 不删除用户保存的密钥。
- 启动时在 `plugin_data/.../tunnel/run-*` 下生成私有配置和 `Bearer` 头文件。目录为 `0700`，配置/密钥为 `0600`。配置仅引用 `file:` 密钥路径，密钥不进入命令行、子进程环境或状态接口。
- 子进程环境采用固定允许列表，避免继承 `CONTROL_PLANE_*`、`MCP_*`、代理或危险 HTTP 原始日志设置覆盖本地配置。此版本没有企业代理或自定义 CA 配置功能。
- 官方 `mcp.extra_headers.Authorization` 用于向本地 MCP 服务带上 Bearer。连接器转发的同名头会覆盖这个静态值；若连接器另设了不一致的 Authorization，必须修正连接器配置，不能把本地 MCP 改为免认证。

## 生命周期和状态

先启动 MCP 监听，再启动 Tunnel；停止或重载时先停止 Tunnel，再关闭 MCP。生命周期方法串行执行，私有文件锁拒绝同一数据目录中的重复进程。子进程继承锁描述符，避免父进程异常退出后直接启动第二个实例。

停止先发送 TERM，超时再 KILL，两次等待均有上限。正常停止、启动失败、取消启动、取消卸载和意外子进程退出都会清理本次生成的文件。若系统拒绝信号或进程无法回收，保留所有权和锁，报告 `stop_failed`，阻止替换进程；随后观察到退出再清理。不会任意查找/杀死其他进程，也不会自动重启。

状态仅返回固定枚举和布尔值，无日志、密钥、Tunnel ID、私有路径或进程原始错误：

- `process_running`：本管理器拥有的进程仍运行。
- `healthy`：本地 `/healthz` 最近返回 200。
- `ready`：本地 `/readyz` 最近返回 200，且 liveness 成功。
- `binary_available`：本次启动已验证匹配的平台、版本与文件哈希；默认关闭时不主动读取执行文件，此值初始为 false。
- `connected: null`、`roundtrip_verified: false`：上述本地健康检查不能证明 ChatGPT 已连通，更不能证明 QQ 收发端到端成功。
- `last_error_code`：固定错误码，不包含异常原文。

启动仅等待本地 liveness，默认最多 15 秒。readiness 可单独保持 false；后台每 5 秒刷新。健康探测只访问随机端口的 `127.0.0.1`，禁用代理和重定向，不读取或回传响应内容。

整个容器被强制终止或宿主断电无法保证 Python 清理代码运行。临时文件仍在私有目录；不得依靠文件删除实现密钥安全擦除。备份需排除临时运行目录并限制持久配置/密钥访问。若只杀死 AstrBot 父进程而保留子进程，管理员需要停止旧运行实例；本模块不会绕过占用锁。

## 官方二进制打包

当前固定版本为官方 v0.0.16。监督模块支持 Linux amd64/arm64；每个发行包必须实际包含对应架构文件，否则安全失败。当前发行包包含哪些架构，以 `vendor` 的实际文件和发行说明为准，不能把模块支持等同于已打包支持。

推荐由发行维护者在制作插件包时完成一次官方验证，不让安装脚本或页面自动从互联网下载：

1. 从官方固定 release 获取 `tunnel-client-runtime-v0.0.16-linux-<arch>.zip`，以及匹配的 `SHA256SUMS.txt`、SPDX、许可证报告和 Sigstore provenance bundle。
2. 使用官方该 tag 的验证流程检查签名/来源、archive checksum、归档成员路径与类型，以及 SPDX payload hash。无法验证的阶段必须在 manifest/发行说明中明确标注；保存签名文件不等于验证过签名。
3. 将窄运行时及许可证材料放入 `vendor/linux-<arch>/`。旁边的 `tunnel-client-runtime.manifest.json` 至少记录 `version: "v0.0.16"`、`platform: "linux-<arch>"`、提取后 ELF 文件的 `sha256`。ZIP 哈希不能代替 ELF 哈希。
4. 不依赖 ZIP 安装器保留执行权限。显式启用后，模块先验证包内 ELF 架构、固定版本与哈希，再复制到本次私有运行目录，设为 `0700`，对副本再次验 hash 后运行。原包可保持 `0644`，不会被 chmod 或改写。禁用时不创建副本。

manifest 是可信发行包的一部分，本地 hash 复核不等于 Sigstore 来源验证；若攻击者能够同时修改插件代码与 manifest，此检查不能建立独立信任根。

官方固定 release 的 Linux archive SHA256：

- amd64：`f8f48017e752810c2c6f02065cdc4ca1ad045b91a6d045cbab144b957fdcbdb1`
- arm64：`ec3bc4815e1b9f7a0773b24de45f64841d9aebe170a58b62742e3f1cef4ac088`

参考：

- [官方 v0.0.16 release](https://github.com/openai/tunnel-client/releases/tag/v0.0.16)
- [固定版本配置文档](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md)
- [固定版本本地健康状态](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/health.md)
- [官方发布验证流程](https://github.com/openai/tunnel-client/tree/v0.0.16#validate-a-downloaded-release)

## 模块验证范围

`tests/test_tunnel_runtime.py` 使用伪进程、合成 ELF 字节和本地测试 HTTP 服务。覆盖默认禁用、认证头文件、无秘密 argv/env/status、平台/哈希失败、暂存时源文件替换、意外退出、启动超时、TERM/KILL、取消清理、重载、重复启动、文件锁、停止失败、符号/硬链接与权限门禁。测试不运行官方二进制，不使用真实凭证，不证明真实控制面或 QQ 通路可用。
