> 当前版本为 v0.5.6；分路配置、最新行为与限制见 [版本说明](UPDATE_0_5_6.md)、[README](README.md)。下文含历史版本记录，旧选项可能已退役；当前 Events 使用 aiohttp，消息读取不自动附带历史。

# v0.3.4 回调代理兼容修正

本版从 v0.3.3 单独修订，不包含尚在开发的图片、文件或历史页面。

## 已修正

以前代理回调只选本地 DNS 返回的第一个公网 IP，没有地址备选。现在优先选择已校验的 IPv4，若代理明确以 CONNECT 502/503/504 拒绝建立隧道，可再选一个已校验的其他地址（优先另一地址族）。最多两次，网络阶段合计最多 10 秒。

认证失败、证书失败、代理连接失败，以及普通断开、重置、超时都不会触发此处地址重试。后几种错误可能发生在请求正文已发送之后，不能因还有另一个地址就再次发送。原有 Events 队列重试策略不变。

仍使用数字 IP CONNECT、原域名 TLS SNI/证书验证及 Host。所有 DNS 地址都须通过公网检查；精确回调域名白名单不变。不会改成代理自行解析回调域名，也不会失败后直连。规则型代理可能对数字 IP 与域名采取不同路径，这一环境差异仍可能需要在代理侧排查。

## 新诊断

qq_status → events.callback_transport 只包含：proxy_enabled、aiohttp_version、最多两个 attempts。每项包含 address_family、固定 error、白名单 errno 名称、数字 http_status。不含 IP、域名、URL、密钥、正文或原始异常。

- callback_proxy_connect_failed：连接指定代理时失败；可能附 ECONNREFUSED/ENETUNREACH/EHOSTUNREACH/ETIMEDOUT/EACCES
- callback_server_disconnected：连接被关闭，不能仅凭此确定发生在 CONNECT 还是目标请求阶段
- callback_connection_reset：连接被重置
- callback_response_incomplete：响应正文未完整读取
- callback_proxy_failed：代理拒绝 CONNECT，可见数字 HTTP 状态
- callback_tls_failed：TLS/证书失败

原有 events.last_subscribe 持久化最后订阅阶段/固定错误；callback_transport 是当前运行期的最近一次回调传输，不是永久网络日志。网络未开始时可能为空。

## 当前已知与未验证

已知现场 v0.3.3 在 callback_verification 阶段出现 callback_network_failed，代理已启用；同一代理的 Tunnel MCP 请求可用。这证明不能笼统归咎于代理整体不可用，但尚不足以定位具体失败原因。

本版修正了可复现的单地址选择限制并增加细分诊断，未连接用户 NAS、真实代理或真实回调，不能声称现场订阅已恢复。原位上传 ZIP 后重新订阅一次，再读取 qq_status；订阅成功后发送一条新的 QQ 消息测试。


## v0.4.0 补充诊断

增加 exception_category/cause_category 固定枚举及少量 errno 符号。特别识别 aiohttp 在 CONNECT200 后 TLS升级阶段包裹的 ConnectionResetError，返回 callback_connection_reset；其他连接器异常返回 callback_connector_failed。不会输出 str/repr 异常、连接键、IP或URL。此分类已在本地假代理复现验证，不能据此宣称现场问题已解决。既有队列有界重试保持，单次调用仍不重发不确定POST。
