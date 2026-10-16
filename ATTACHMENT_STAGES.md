# 附件读取阶段（v0.4.10）

本版用于区分附件URL查询、域名解析和文件下载超时，不增加原有超时，也不声称已经定位用户现场根因。

qq_status 的附件摘要提供实际运行 attachment_dns 与 proxy_enabled，以及有界的最后一次读取诊断。qq_read_attachment 失败时可附 transport_diagnostic。诊断仅含固定阶段、耗时、配置枚举/布尔值和固定API动作等，不包含消息正文、QQ号、源URL、路径、query、认证头或原始异常文本。错误里的DNS诊断仍按v0.4.9提供hostname。

阶段含义：
- queued：等待附件读取锁。
- source_resolution / adapter_api：获取来源地址；api_action可区分get_image、get_private_file_url或get_group_file_url。
- dns：当前附件域名解析；失败时同时参考dns_diagnostic。
- connection_or_request：连接建立和请求发送期间，可能包含代理CONNECT与TLS握手。本阶段不单独证明CONNECT或TLS成功。
- request_or_response_headers：连接已取得，进入请求头写入/等待响应阶段；aiohttp追踪点不证明请求已到服务器。
- response_headers：已收到响应头，正在验证状态与限制。
- response_body：已收到响应头，正在读取响应体。
- cache_io / result / complete：缓存、结果编码或完成。

elapsed_ms是本次读取的单调时钟耗时，阶段切换及API结果用于定位，不代表远端服务处理耗时。代理与DNS值来自运行实例，不仅是页面中尚未应用的设置。并发读取只保留最近开始的诊断，旧请求不能覆盖新请求。

## 图片API的有限后备

原先get_image辅助查询可能耗尽整个20秒来源解析期限，导致即使消息已经携带HTTPS地址也来不及尝试后备。现在仅当图片原始捕获地址通过HTTPS结构检查时，该辅助API等待最多3秒；取消完成后重新验证当前消息、机器人、DNS及代理设置，再使用原始地址。API及时返回可用地址时仍优先使用API结果。

后备地址仍须通过完整DNS公网检查、IP固定、TLS证书验证、无重定向和大小限制；结构合法不代表网络可用。没有合法原始HTTPS、文件来源及私聊HTTP短期授权不采用这一缩短策略。不会后台重试API、放宽HTTP图片、改写签名链接或自动直连。

若适配器自身抑制取消，Python不能强制终止该协程；插件不会在取消尚未完成时同时启动后备下载。云端测试覆盖正常取消完成后才使用后备，以及取消过程中撤权立即拒绝。

现场操作：原位更新、保持现有已应用配置，重新发送一张图片供读取。如果失败，读取工具错误和qq_status即可查看固定阶段，无需输出带签名的下载URL或重新填密钥。
