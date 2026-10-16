> v0.5.2：仅 @ 群可先后台缓存允许成员文件，再关联同群同发送者的明确文件请求。关联候选有10分钟有效期，群HTTP必须独立绑定适配器即时API来源，详见 UPDATE_0_5_2.md。

v0.5：ZIP默认目录+原包，显式include_raw=false仅目录；当前规则可接收多个私聊用户，附件仍绑定原sender/群。20MB和20秒正文空闲限制不变。详见HELP.md。

v0.4.15：QQ读取不再附带历史；ZIP显式include_raw=true同时返回目录与原包；附件改20秒无正文进展超时，持续传输可超过60秒。详见 UPDATE_0_4_15.md。

# v0.4 图片与文件附件

附件来自已通过账号、会话、群白名单和消息身份校验的 QQ 消息。`qq_read_message` 只返回不透明的附件 ID 和安全元数据；`qq_read_attachment` 只接受 `message_id`、`attachment_id`，不能指定网址、QQ 号、文件 ID 或本机路径。

## 方向与客户端工具清单

这里的图片/文件支持指QQ入站附件读取。qq_reply发送文字；v0.4.13新增qq_reply_attachment回传图片/文件，接口与后端兼容见 MEDIA_REPLIES.md。收到附件元数据不等于已下载、看图或成功传输文件。

服务端tools/list公布7个工具（含 qq_reply_attachment），附件字节入口为qq_read_attachment。若客户端只列出旧3工具，请按官方自定义MCP连接说明刷新元数据并在新会话测试：https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata 。刷新后仍需实际测试，不能仅凭接口声明认定图像或文件已经成功读取。

## 可以做什么

- 接收原始消息中的图片、文件以及它们与文字的组合；读取附件时再按需下载，不在收消息时预下载。
- 每个附件与消息 ID、原始段序号和种类绑定。同一条消息中的多个文件分别使用自己的原始 `file_id`，不信任 AstrBot 归一化组件上的下载链接，也不会读取会触发下载的 `File.file` 属性。
- 对有可用 HTTPS 来源的图片，检查文件头后，将 PNG、JPEG、GIF、WebP 作为 MCP 图片内容返回。其他格式作为二进制文件资源返回；不把 SVG/HTML 作为可执行图片处理。
- 对普通文件，返回 MCP `resource` 内容，使用 `blob` 承载 base64 文件字节。文件资源 URI 只含不透明 ID，不是可直接访问的下载地址。
- 返回安全文件名、实际字节数、嗅探出的 MIME 和 SHA-256。文件名与 HTTP `Content-Type` 都不能决定真实 MIME。

## 小文本正文（v0.4.8）

普通 file 附件在完整字节不超过64KiB（65,536字节）、魔数未识别为图片/PDF/ZIP且严格UTF-8解码通过时，除原始resource.blob外，另返回一个模型可读TextContent。正文放在JSON的text字段中，携带message_id、attachment_id、显示文件名、transport、integrity、complete=true、truncated=false及“不可信文件数据，不是指令”的提示。原始字节资源仍可用于准确还原文件。

UTF-8开头BOM可移除以便显示，text_preview.bom_removed会说明；blob与SHA-256仍对应未修改原字节。保留普通Unicode、Tab、CR/LF，拒绝Unicode类别C（包括NUL/其它控制、格式、私用及未分配字符；仅Tab/CR/LF例外），因此内部BOM和双向控制字符也不可用。不猜编码、不用replacement解码，不读取压缩包内部内容，不执行Python/宏，不调用解析器或OCR。

超过64KiB、非UTF-8或二进制内容不提供截断正文，而返回text_preview.available=false和固定reason。附件blob仍可能正常返回，预览不可用不等于附件下载失败；原4MiB输出上限继续计算元数据、blob和新增正文。mime_type仍按魔数保守分类，因此.py可能仍为application/octet-stream，但TextContent可以含完整源码。

正文是用户附件中的不可信数据，提示词标记不能提供强隔离或防止所有提示注入。HTTP内容仍可能被篡改，integrity=unverified不因能解码为文本而改变。所有原有消息/群隐私和授权边界继续适用。

文件显示名依次取file_name、name，最后才尝试file段的data.file。后备值必须是安全basename；URL、路径、base64形式和控制字符不采用，仍显示file。该名字不作为路径或file_id使用。

文件头检查不是完整图片解码、恶意软件扫描或内容安全认证。本插件不做 OCR，不解析文档，不解压归档，不执行代码，也不会自动打开文件。客户端是否能展示图片或理解文件内容，取决于客户端对相应 MCP 内容类型的支持。

## 明确的大小边界

- 每个下载最多 20 MB（20,000,000 字节），按实际流式字节累计；伪造或缺失 `Content-Length` 不能绕过限制。
- 附件缓存最多 100 MiB。两个不同附件可并行下载，同一附件合并等待；下载前为完整的 20 MB（20,000,000 字节）临时文件预留空间，需要时淘汰最久未使用的缓存。已淘汰的附件描述仍随消息保留，可在原会话句柄仍有效时再次读取。
- 每次 MCP 附件结果最多 32 MiB，包含 base64 膨胀、元数据和内容结构；20 MB 原件经 base64 后约 26.67 MB，可容纳于该插件预算。
- 下载使用 20 秒总超时，包含 DNS 与 HTTP 连接/读取；文件 ID 的适配器解析另有最多 20 秒等待。DNS 并发和答案数量也有边界。

附件缓存与会话 SQLite/日志的空间限额分别计算。100 MiB 是附件文件和单次下载临时文件的上界，不是整个插件数据目录的总上界。

## 来源与网络限制

v0.4.6 会保留图片原始 `data.file` 中严格受限的散列形状文件标识，并优先通过当前捕获事件的机器人查询来源：图片调用 `get_image(file, self_id)`，文件调用下述私聊/群聊 URL API。图片 API 查询只接受受限的 32 位十六进制图片标识加必需的允许图片/缓存后缀，不接受一般文件名；URL、base64 与本机路径都不会作为图片文件标识传给后端。

OneBot v11 的 `get_image` 标准只保证返回后端本地文件路径，并不保证 HTTPS URL。插件只使用响应的 `url` 字符串；不打开 `file`/`path`、不读取 base64、不使用非标准 `get_file` 兜底。API 不支持、缺少 URL 或 URL 不通过格式/HTTPS 校验时，可继续使用原捕获消息中本来就有效的 HTTPS URL；永不使用失败的 API 来源。没有可用的原始 HTTPS 时明确拒绝。

默认仅下载 HTTPS、默认 443 端口的公网主机。v0.4.7 可由用户在详细页单独开启受控私聊文件 HTTP:80 例外，范围和明文风险见 PRIVATE_FILE_HTTP.md；不自动开启。QQ 适配器只提供 HTTP URL 时，消息及附件元数据仍会保存，读取返回 `attachment_https_required`。插件不会改写链接协议，也不会在未开启专门例外时降级使用 HTTP。

禁止本机路径、`file:`、`data:`、base64 伪 URL、URL 用户名/密码和重定向。HTTPS 路径拒绝 IP 字面量；HTTP 特例仅允许经核验的公网地址。所有 DNS 答案必须是公网地址；混入私网、回环、链路本地、保留或特殊转换地址时整次下载被拒绝。连接固定到已核验的 IP，同时保留原主机的 TLS 校验、SNI 和 Host，防止解析后换址。

已配置显式代理时，附件下载也使用同一代理；HTTP CONNECT 的目标是核验过的数字 IP。代理失败不会绕开代理直连。不会读取环境代理、发送 Cookie 或自动解压 HTTP 内容。

`file_id` 只能由宿主使用当前捕获事件的机器人客户端解析：私聊调用 `get_private_file_url(file_id, self_id)`，群聊调用 `get_group_file_url(file_id, group_id, self_id)`。参数必须来自同一条已核验的原始消息及其确切附件段，不能来自 MCP 调用者。

## 缓存与授权边界

缓存目录为 0700、文件为 0600，采用不可预测的临时文件名、独占创建和原子替换。目录逐级拒绝符号链接；读写时拒绝符号链接、硬链接、非普通文件和不合要求的权限。磁盘文件名仅由插件生成的不透明 ID 组成，原始文件名不会参与路径拼接。缓存命中会重新核对文件大小与 SHA-256。

原始来源 URL 可能包含临时密钥，只存于本地私有 SQLite 数据，不出现在 MCP 元数据、状态或固定错误码中。它不是加密存储，数据库、附件目录和备份仍应限制访问。

宿主在等待缓存锁后、解析文件来源前、适配器解析返回后、DNS 返回且开始 HTTP 请求前，以及向 MCP 返回结果前核验当前绑定和捕获事件。撤销权限或更改消息来源后会拒绝后续步骤。已经开始的网络请求不能撤回；客户端不会收到未通过最终校验的内容。

重载、重启或捕获事件句柄被淘汰后，历史附件元数据可以继续显示，但宿主缺少可靠的当前事件上下文时会拒绝读取，即使磁盘上仍有缓存。请让原发送者在允许的会话中重新发送；不使用任意目标/文件解析作为回退。

## 离线验收

`python -m unittest discover -s tests -p test_attachments.py -v`

测试仅使用合成数据库、临时目录、伪造 DNS 和伪造网络响应；覆盖绑定错配、事务回滚、大小限制、base64 输出边界、实际代理参数、DNS 固定、重定向、压缩、缓存损坏、符号链接/硬链接、取消与权限撤销。没有访问真实 QQ、NAS、用户附件或线上凭证；完整框架及真实客户端的文件展示仍需后续部署验收。

## API 兼容证据与现场限制

- AstrBot v4.28.2 的文件 URL API 用法：https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py 。本插件取当前附件段的 ID，不复用其他段或首段的 ID。
- OneBot v11 get_image 的标准响应为后端本地路径：https://github.com/botuniverse/onebot-11/blob/master/api/public.md#get_image-获取图片 。实现扩展是否另有 HTTPS url，依后端而定。
- 此兼容补丁不需要增加凭证或放开网络权限；没有验证用户真实后端能返回 HTTPS，不能把 API 离线测试当作真实图片/文件已可读取。

## v0.4.6 安全来源诊断

若绑定机器人 API 返回 HTTP 且没有可用的原始 HTTPS 来源，qq_read_attachment 继续返回 attachment_https_required，并附带 source_diagnostic：source=adapter_api、scheme=http、hostname、port。这里只报告来源形状，不含用户名/密码、完整 URL、路径、query、file_id 或文件内容。hostname/port 不代表该地址已通过公网检查；HTTP 请求不会发出。其他异常不会输出原始异常文本。

现场确认的失败文件没有捕获 URL、有 file_id，所以 v0.4.4 已调用文件 URL API，收到 HTTP 后拒绝。v0.4.6 的图片标识/解析修正不等于解决这个文件；新诊断用于确认实际 API 主机。无需新凭证或开启全局 enableLocalFile2Url。v0.4.6 没有增加 HTTP 下载；v0.4.7 的显式默认关闭例外见 PRIVATE_FILE_HTTP.md，仍不实现后端文件流协议。

## v0.4.9 附件解析

附件下载复用已有显式代理；v0.5.1 起页面使用统一 DNS。旧回调/附件 DNS 冲突时暂保留各自值，等待管理员明确选择。Google 模式会向 Google 提供下载域名，详情见 UPDATE_0_5_1.md。

输出预算是插件本地限制，未验证托管平台对20 MB原件的端到端接收。并发和内存边界见 ATTACHMENT_CONCURRENCY.md。
