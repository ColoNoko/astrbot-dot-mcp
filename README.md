# AstrBot 插件内置 dot MCP v0.3.2（文本原型）

这是 AstrBot 原生插件：在插件中启动独立 HTTP MCP 服务。AstrBot 只负责接收 QQ 消息和调用已有适配器回复；不调用 AstrBot 的 MCP 客户端、模型或默认 LLM，也不新增桥接容器。针对 AstrBot v4.28.2 源码核对，尚未实际加载完整框架。

v0.3.0 提供 AstrBot 原生侧栏插件页面：平台、内置 MCP、Tunnel 和状态集中配置。基础设置只显示启用、我的 QQ、群白名单和群回复模式；旧高级配置键保留并隐藏，更新不会删掉原密钥。图标沿用原 PNG。

先看 QUICKSTART.md；原位更新见 UPGRADE.md；群回复的后续自动化规则见 GROUP_REPLY_RULES.md。

## 范围

- 绑定一个平台实例、适配器、机器人账号和本人账号。支持本人私聊。群模式 `owner_at`（旧版兼容默认）只收本人真实 @ 机器人的文字；显式选择 `all_messages` 后，白名单群内任何非机器人账号发普通文字都可触发，无需 @。默认关闭，群白名单默认空，升级不自动扩大模式。all_messages 不验证小号归属，也会接受今后加入白名单群的其他非机器人账号；群成员变化时应重新评估权限。
- 本原型只允许现有的 `aiocqhttp` QQ 适配器，账号 ID 须为十进制数字。`qq_official` 暂不支持：它的原始路由字段及 Markdown/重试路径需要单独验证。不会替用户更换、安装或配置现有适配器。适配器名称是代码门禁，尚未验证真实 QQ 兼容性。
- 精确匹配后先 `event.stop_event()`，再写入 SQLite，避免此插件之后的默认模型重复回答。优先级 100 不能撤回更早执行的其他插件已经发出的消息；部署时须排查其他抢先处理器。
- 非白名单群和机器人自身消息始终忽略。图片、语音、文件、引用回复、转发和 @其他人/@全体等组件不支持；文本与它们混合也拒绝。owner_at 模式还拒绝其他成员及没有真实 @ 的消息。all_messages 不需要 @，文字形式的 @ 只是普通内容。
- SQLite 保存消息、观察到的 UMO、原始消息 ID、会话类型/群号/发送者、回复幂等键、事件订阅和投递状态。消息去重键包含会话类型、群号、发送者和原始 ID，避免跨会话碰撞。MCP 只能按不透明消息 ID 读/回复，没有任意收件人参数。
- 回复使用捕获的原始事件 `event.send(event.plain_result(text))`，在接收及发送前重新核对完整绑定、UMO 和原始适配器消息的 self_id/user_id/会话类型。只接受真实 `aiocqhttp.Event`，防止普通字典在适配器发送路径丢失 self_id。私聊原始 message 数组必须全是 text；群聊只允许 text 和可选的、目标正是机器人的顶层 at；owner_at 另要求至少一次真实 at。原始 sender.user_id、顶层 user_id、归一化 sender 必须彼此一致，并按当前模式核对权限；all_messages 记录实际发送者，始终拒绝机器人自身。群号必须与原始/归一化路由、当前白名单一致。原始和归一化消息 ID 必须一致。保留原始文字空格，并核对每个连续文本段的 strip 归一化规则。CQ 字符串按普通文字处理。
- 本原型最多保留最近 1000 个回复事件句柄。重载/重启或句柄淘汰后，历史消息仍可读取，但无法回复，返回 `not_sent`；请让该发送者发一条新消息。插件不调用 `context.send_message` 主动发送回退。

## 安装与配置

1. 备份现有插件配置与数据。在插件管理「从文件安装」上传 ZIP；同名包支持原位替换，保留既有目录身份，不必卸载。具体步骤和回滚边界见 UPGRADE.md，不要覆盖整个 AstrBot 安装。
2. 插件依赖只有 `aiohttp` 与 `standardwebhooks`（见 requirements.txt），由现有 AstrBot 插件依赖机制安装。最低 aiohttp 要求为 3.11.18，已在隔离环境测试，避免仅因插件最低版本要求而强制升级 4.28.2 的共享 aiohttp。不会安装、升级或导入 `mcp` SDK，不影响 AstrBot 的 `mcp<2` 约束。
3. 保持 `enabled=false`。完整启动后的自动发现仅填写空白 `platform_id`、`adapter`，且要求已加载实例和已启用配置两边都只有一个匹配候选；多候选/失败实例造成歧义时不选第一项。可在本地日志看到有界的候选实例 ID。已运行后安装或重载时，为避免启动中的部分列表误选，空白字段保持原样；手填或完整重启一次即可。自身 QQ `self_id`、本人 QQ `sender_id`、群白名单 `group_ids` 均需手填，不使用随机 client_self_id 或第一位发消息的人推断。已有填写不覆盖。
4. 本机 MCP 认证由插件自动管理：用户显式启用/应用后首次启动自动保存，旧密钥继续复用。无需手填或点击生成。`secret:true` 只遮罩配置界面，磁盘上的配置和 SQLite 订阅密钥不加密，应限制文件访问和备份访问。
5. 设置实际 dot 回调 HTTPS 主机的精确允许列表；不能填通配符、协议、端口或路径。未列出的主机不能订阅。地址须解析为公网 IP，不允许 HTTP、重定向或私网目标。
6. 默认监听容器内 `127.0.0.1:8769`，端点 `/mcp`。仅在理解网络边界后显式改成 `0.0.0.0` 并配置受保护的 HTTPS 反向代理。外部工具使用 `Authorization: Bearer <专用密钥>`。此旧直连方式仍兼容已有配置；新版管理页仅提供 loopback。内置官方 Tunnel runtime 可在用户明确保存并应用后启动，使用已有 Tunnel ID 和用户本地输入的 runtime key；不会创建 OpenAI Tunnel/账号凭证或部署到其他机器。
7. 如需已确认小号群里的全部普通发言，手动设置 `group_trigger_mode=all_messages` 并保存；旧配置保持 owner_at。确认参数后开启，用对应模式的正例验收，再检查非白名单群、机器人自己及引用/媒体不进入桥接。连接 dot 及发送真实消息的步骤尚未执行。

路径仅作说明：若现有安装位于 `/srv/astrbot`，必须先核对它与容器 `data` 的现有挂载关系；不要按该路径重新创建或替换用户安装。数据放在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_dot_private/inbox.sqlite3`。绑定变更时拒绝复用旧数据库，请先停用并备份，手工确定旧私密数据的处理方式。

## 升级旧数据库与群隐私

v0.2.0 → v0.2.1 不改变 SQLitev2 数据格式，既有配置键、群白名单、消息和回复去重键继续保留。新增模式缺省为 owner_at；上传新版不会自动切到 all_messages。

从 v0.1.x 升级时，在停用并备份后启动新插件：数据库在一个事务内将旧消息标记为 private、保留原消息 ID/回复键/事件引用，并改为跨会话复合去重。迁移失败会回滚。不要用旧插件直接打开新数据库；降级须先恢复备份。没有修改用户实际数据库，迁移测试只使用合成数据。

qq_read_message 明确返回 conversation_type、group_id、sender_id、privacy_scope。群回复只能基于当前群公开上下文，私人资料或其他群内容要取得具体披露批准。固定路由及这些字段不等于模型记忆隔离；同一认证客户端能读取已接收的不同会话。本次没有设置自动化，后续须采用 GROUP_REPLY_RULES.md 的限制。

## AstrBot 每周升级检查

不锁死 AstrBot 版本，也不修改框架源码。启用时检查必需的公开事件方法与数据目录 API；接收和发送时检查真实事件类型、原始路由、纯文本、消息 ID 和归一化一致性。缺少这些可检测能力时拒绝启用或拒绝该消息。接口名字不变但内部语义改变，仍可能逃过检查，因此不能保证所有未来版本自动兼容。

每次升级先保持插件关闭，备份配置和 plugin_data；确认会话已启用本插件，停用该绑定私聊与白名单群中其他会抢先回答的处理器。然后开启，用绑定账号发纯文本检查只回复一次；按所选模式检查账号及 @ 条件，并确认非白名单群和机器人自身消息不被拦截；测试引用、媒体/混合组件确认不进入桥接；检查日志中没有启动错误，再恢复日常使用。升级不是要求重装或替换已有适配器。

4.28.2 仪表盘保存插件配置会自动重载插件。直接编辑配置文件后须手动重载。若运行中的 config 已被禁用或绑定改变，下一次接收/发送检查会立即拒绝；不能撤回已经进入适配器网络请求的消息。

## 内置 MCP 接口

独立的有界 HTTP JSON-RPC 实现，使用 `2026-07-28` 所需的发现、工具和自定义 Events 子集：

- `server/discover`
- `tools/list`、`tools/call`
- `events/list`、`events/subscribe`、`events/unsubscribe`
- 工具 `qq_read_message`、`qq_reply`、`qq_status`

它不是完整 MCP SDK 或完整协议一致性实现；不把资源订阅当作 dot 的事件唤醒。自定义 webhook 订阅使用签名挑战验证和 Standard Webhooks 签名；消息与待投递事件同一数据库事务写入。首次订阅前的历史消息不补发，断线后的已入队事件有界重试，接收方仍须按稳定事件 ID 去重。

返回 `adapter_completed` 只表示适配器调用正常返回，不证明平台接受或 QQ 送达；适配器可能内部吞掉错误、重试或回退。插件幂等不能阻止适配器内部重复请求。`not_sent` 表示已知未进入发送；`uncertain` 表示可能已经发出，禁止自动重试相同幂等键。新的幂等键会产生新的发送意图，调用者不得用换键绕过不确定状态。超时和关闭会撤销等待中的发送权限；已开始的适配器网络请求无法撤回，也不能保证对端没有收到。若第三方适配器吞掉取消并永久挂起，卸载等待也可能挂起；没有强杀 AstrBot 进程的逻辑。

## 验证与限制

见 TEST_REPORT.md。测试为云端 mock 适配器、本地 HTTP 回调及协议类型验证。没有实际加载运行用户的 AstrBot，没有真实 QQ、用户 NAS、手机、dot 或外网反向代理端到端验证。此版本为有限范围验证原型，不是生产安全认证。

## 官方依据

- [AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)
- [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [插件存储](https://docs.astrbot.app/dev/star/guides/storage.html)
- [AstrMessageEvent 源码](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/platform/astr_message_event.py)
- [Star 生命周期](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/star/base.py)
- [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)
