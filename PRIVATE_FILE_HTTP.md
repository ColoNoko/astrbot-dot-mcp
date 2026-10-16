> v0.5.2 增加独立的群文件 API 受控 HTTP 授权，随群文件缓存总开关关闭；并未把下述私聊开关扩展为群权限。群来源、匹配与明文风险见 UPDATE_0_5_2.md。

# 私聊文件 HTTP 兼容（v0.4.7，默认关闭）

## 为什么需要单独开关

部分 OneBot 后端的私聊文件 API 正常返回 `http://公网地址:80/...`。例如已核对的 NapCat 源码，从 QQ 协议响应的 server、port 和 url 构建 HTTP；邻近的群文件 API 使用另一条 HTTPS 路径。这不证明某个现场 IP 的所有权，也不证明把私聊链接改成 HTTPS 就能工作。

默认仍拒绝 HTTP。这个版本提供一个明确的局部兼容选项，不更改 NAS 全局网络、回调 DNS、TLS 验证或其它附件来源。

## 如何选择

在插件详细“连接设置”页面，阅读“允许私聊文件 HTTP（有风险）”说明。确实接受明文风险时手动勾选，保存后应用。升级不会自动打开；旧配置缺少该键时按 false 处理。无需更换 API key，也不要求启用后端全局 enableLocalFile2Url。

保持关闭时，私聊 HTTP 文件继续返回 attachment_https_required 及安全来源诊断。开启后也不保证所有后端都能返回有效文件。安装/应用会重载插件，请重新发送小测试文件，再在已包含六个工具的新会话中读取附件。

## 许可范围

- 只用于当前捕获的本人私聊消息中的 file 附件，不适用于图片或群文件。
- 必须有该消息确切附件段的 file_id，并经原绑定机器人客户端和 self_id 即时调用 get_private_file_url。
- 模型不能传 URL、IP、file_id、路径或下载选项；消息段直接携带的 HTTP URL 不能获得例外。
- API 返回来源经过短期、单次读取的宿主许可；停用插件、关闭选项、改变绑定/消息/机器人句柄都会使后续步骤失败。
- 只接受 HTTP 的 80 端口及公网目标。域名的全部 DNS 答案须通过公网检查；连接固定到已校验 IP。公网 IP 字面量也须单独检查。不会固定某个现场 IP 为永久可信目标。
- 无重定向、Cookie、源站 Authorization、MCP token、回调签名头或自动解压；不打开本机路径。现有显式代理继续适用，代理认证只用于代理；代理失败不绕过它直连。
- 仍有每文件 20 MB（20,000,000字节）、下载总预算60秒/连接与空闲10秒（原私聊HTTP授权grant仍30秒有效）、缓存 100 MiB、MCP 输出 32 MiB 的边界。内容不执行、不解压、不自动打开。
- HTTP 缓存保留明文来源标记，不能在关闭开关后绕过策略继续读取；每次读取仍需要有效捕获事件及新的绑定 API 许可。

## 无法消除的风险

HTTP 没有源站证书校验或传输加密。下载链接中的临时访问参数及文件内容可能被中间网络或代理看到；内容可能被替换。限制目标、端口和来源可以减少任意网络访问风险，但不能补上 TLS 的保密性和真实性。

正常 OneBot 文件事件和 get_private_file_url 没有提供可直接信任的预期 SHA-256。下载后计算的 SHA-256 只是收到字节的指纹，不能证明这些字节就是发送者的原件。返回结果会标注 HTTP 来源和完整性未验证。不要据此自动运行代码、打开活跃文档或作高风险决定。

内部字段 file10MMd5 可能仅覆盖前 10 MiB；fileHash 的名称也不能证明它是全文件散列。本插件不会凭字段名称拼凑“端到端校验”。

## 官方源码依据

- 私聊/群文件 URL 构建：https://github.com/NapNeko/NapCatQQ/blob/3f66b369017cf1e202583257baa0c19406cd186c/packages/napcat-core/packet/context/operationContext.ts#L241-L252
- 私聊 API 只返回 URL：https://github.com/NapNeko/NapCatQQ/blob/3f66b369017cf1e202583257baa0c19406cd186c/packages/napcat-onebot/action/file/GetPrivateFileUrl.ts#L8-L43
- 普通文件消息字段：https://github.com/NapNeko/NapCatQQ/blob/3f66b369017cf1e202583257baa0c19406cd186c/packages/napcat-onebot/api/msg.ts#L169-L216

这些是指定版本的源码核查，并非用户现场后端的认证或 IP 归属证明。云端测试只使用合成文件、本地伪代理和测试服务器，没有开启用户开关、访问其文件 HTTP 地址或部署 NAS。
