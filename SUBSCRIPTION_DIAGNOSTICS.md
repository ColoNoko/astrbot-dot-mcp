# v0.4.4 响应编码与挑战诊断

标准aiohttp现在明确请求Accept-Encoding: identity。旧版禁用自动解压却保留默认压缩协商，可能把压缩JSON当作普通JSON而报challenge_failed。若服务器忽略identity并返回压缩编码，或重复Content-Encoding，返回callback_encoding_unsupported，不尝试无界解压。

callback_transport.attempts[].response_encoding仅返回identity/gzip/deflate/br/zstd/other，不回显未知头值。callback_transport.verification只包含以下布尔值，原生日志也输出这些布尔项：json_valid、object_response、challenge_present、challenge_string、challenge_match、within_deadline、status_2xx。编码在解析前被拒绝时不会生成verification。

必须同时满足2xx、正确challenge常量时间比对和短期有效性才能激活；HTTP200本身不表示验证成功。不会记录challenge、任意JSON键名/值、原始正文/头、URL或签名密钥。本修复已在本地真实HTTP复现验证，现场结果仍需一次重试确认。

# v0.4.2 原生日志与TLS分类

AstrBot原生日志会输出 dot_private_subscription 安全摘要，订阅结果共用每30秒最多一条限额（重新加载后重置）。日志失败不影响协议处理。被限流时可直接读取 qq_status 的 events.last_subscribe 和 callback_transport。

callback_transport.attempts 新增：
- tls_category：certificate_verification / unexpected_eof / zero_return / ssl_error，非TLS异常为null。
- tls_verify_code：仅证书校验异常提供0至65535整数，其他或异常值为null。62通常表示主机名不匹配；应结合运行环境验证，不能据码自动更改证书校验。
- tls_reason：certificate_verify_failed / wrong_version / protocol_version / handshake_alert / certificate_alert / unexpected_eof / other；未知原因仅返回other。

不会返回原始reason、verify_message、str(exception)、证书主体、回调地址、签名、密钥或消息。TLS失败须根据分类进一步判断；本版没有关闭验证或无证据修改网络安全策略。

# v0.4.1 回调阶段诊断

qq_status 的 events.callback_transport.attempts 每项新增：
- phase：微信兼容后端为 proxy_tcp/target_tcp、connect_response、tls、request_write、response_headers、response_body；starting 表示尚未进入后端。标准 aiohttp 后端仅可报告 connection_or_request 与 response_body，不伪称细分TLS阶段。
- connect_status：代理CONNECT响应码；200只说明隧道接受，不代表目标TLS或回调成功。
- response_status：目标HTTP响应头状态；即使正文超时也保留。null不能证明没有网络连接。
- request_body_complete：HTTPCore观察到完整应用POST正文写入；不代表对端已处理。aiohttp未观测时为null，不能据此断定未发送。
- elapsed_ms / attempt_budget_ms：该网络尝试经过时间与起始预算（毫秒）；DNS预算另计。所有尝试共享10秒截止时间，清理/调度可能使观察耗时略超预算。

callback_timeout表示本地网络交换期限耗尽，并不等同代理TCP连接超时。challenge_failed才是收到完整回应后验证内容/状态不符合要求。HTTP状态与阶段均不含URL、密钥、签名、头或正文。现场故障仍需结合阶段和对应时间代理日志判断。

# 订阅失败诊断（v0.3.3）

本版只增加安全可观察性，不猜测现场原因，也没有改变订阅地址许可、TLS或代理策略。工具 qq_status 可读取：

- subscribe_http.recognized_requests：本次进程中，认证通过且JSON可解析、method明确为 events/subscribe 的请求数量。不是所有网络尝试数量；0不能单独证明客户端没尝试。
- subscribe_http.last_stage / last_error：请求形状或 protocol_metadata 阶段拒绝也能被记录。invalid_params_or_metadata 不包含原始参数或头。
- http_rejections：本次进程的固定HTTP拒绝类别计数，例如 unauthorized/origin_not_allowed/json_required。无法将任意一次拒绝强行归因到订阅；只提供排查线索。
- events.last_subscribe：最近一次进入Events逻辑的尝试次数、时间、阶段、固定错误码、HTTP状态。按当前绑定只保留一条，重启后仍保留。不会保存完整callback URL、签名secret、请求/响应正文、QQ内容或原始异常。
- events.callback_proxy_enabled：当前回调传输是否配置了代理。

阶段：validate_request → waiting_for_callback_lock → callback_verification → persist_subscription → active。

常见固定原因：
- callback_not_allowed：回调主机未在精确白名单中。
- callback_dns_failed / callback_dns_blocked：本地解析失败或解析到非公网地址；HTTP代理不跳过这一步。
- callback_tls_failed：TLS握手或证书验证失败，须结合tls_category/verify_code进一步判断；不会关闭验证。
- callback_proxy_failed +407：代理拒绝认证；其他状态表示代理CONNECT拒绝。不可由旧版generic network错误反推407。
- callback_network_failed / callback_timeout：网络失败或超时，不能单凭代码断言具体故障设备。
- challenge_failed +HTTP状态：收到回调响应，但非2xx、challenge未原样回显、响应不合法或验证时限超出。不会记录响应正文。
- invalid_params / invalid_signing_secret：输入不满足已实现协议。
- event_operation_failed：未分类内部错误，原始错误保持不公开。

使用：更新后读取一次状态，明确重试一次订阅，再读取状态。更新前失败没有记录，不能回溯。成功需要 active_subscriptions>0；随后发送一条新的被允许QQ消息验证唤醒，旧消息不会自动重放。

这些诊断不是故障修复承诺。仍未验证用户NAS/真实Tunnel/QQ订阅闭环。图片/文件与10MiB滚动历史已随v0.4.0提供，本诊断补丁保持其功能与限制。

## v0.4.3 DNS诊断

Google模式的 callback_transport.dns 记录mode、phase、固定error、elapsed_ms和受限HTTP/CONNECT状态；订阅日志也带对应DNS摘要。解析失败时callback_transport.attempts可以为空，这表示尚未开始回调POST。GoogleDNS成功不表示后续TLS或挑战验证必然成功；仍查看最后回调尝试阶段。DNS答案IP、完整URL和密钥不会写日志或状态。
