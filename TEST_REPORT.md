# v0.3.2 测试报告

日期：2026-10-14（UTC）。测试位置：隔离的云端工作目录。用户的电脑、NAS、AstrBot 实例、QQ 账号均未访问或修改。

## v0.3.2 代理与内部认证验证

- 5 项共享代理配置校验：默认关闭、HTTP/端口/URL边界、无效scheme/内嵌凭据拒绝、单独认证编码与repr脱敏。
- 15 项回调代理测试，覆盖挑战与事件同配置、数字IP CONNECT authority、原域名 Host/SNI、正确证书通过/错误证书拒绝、proxy认证不进入目标请求、失败不直连、响应限制/重定向/私网DNS继续拦截。
- 回调线级测试只在云端 loopback 合成代理和临时测试TLS证书上运行；fake代理不连接任何远端目标，未使用用户密钥。
- Tunnel共30项mock进程测试，新增9项验证child-only CONTROL_PLANE_HTTP_PROXY、全局环境不变、本地MCP和健康直连、失败不改直连、认证不进argv/YAML/status。
- 管理API测试覆盖代理认证保留/清除、换地址重填防误发、无效已存URL凭据不回显；内部MCP GET零生成、明确启动首次保存、失败保持关闭、重启复用不轮换。
- 前端26项DOM模拟测试包含移除手动MCP认证控件、代理表单/错误反馈/提交边界、中文ID与原安全逻辑。
- 未运行真实官方Tunnel二进制、未连接任何用户或公网代理、未访问NAS。真实浏览器12项仍未执行，不能声称实际UI/Tunnel/QQ/dot闭环验收。

## v0.3.1 中文实例名修复

AstrBot 4.28.2 的“机器人名称”直接作为平台实例 ID，aiocqhttp 的 meta().id 取自 config.id。v0.3.0 错误地把实例 ID 限制为 ASCII，中文实例会在发现、保存和下拉列表三个环节被排除；这是插件兼容缺陷，不需要重建或重命名已有机器人。

现支持原 ASCII 字符集及可打印、无空白的 Unicode 字符，最多 128 个 Unicode 码点。保留原字符串精确匹配，不做大小写折叠、Unicode 归一化或截断；控制/隐形/代理字符仍拒绝。错误的配置 ID 不再通过去控制字符变成另一个合法绑定。

直接上传 v0.3.1 ZIP 原位更新，重新打开连接页面/刷新状态，选择已有中文实例。全部配置键、数据库及 Tunnel 二进制不变。云端合成用例已验证中文发现/保存/事件匹配；尚未访问用户 NAS 或验证其实际页面刷新结果。

官方源码：[机器人名称字段](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/config/default.py#L893-L897)、[aiocqhttp 元数据](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L48-L53)。

## 已测范围

1. Host/SQLite 单元测试：四项绑定、私聊/群聊隔离、仅 Plain 文本、错误类型/布尔 ID/空白控制符身份拒绝、重复消息、重启恢复、绑定变更阻止、原始消息 ID 与观察到的 UMO 持久化、原始适配器 self_id/user_id/私聊路由一致性及发送前再验证、未知消息和幂等键冲突。
2. 原生插件入口的 API 形状 mock：优先级私聊处理、只有匹配的有效文本会停止事件、停止早于入库、普通文本回复、配置/UMO 再验证、默认禁用、非 QQ 适配器拒绝、重启后旧句柄明确未发送。
3. 生命周期：启动错误不泄漏原始异常/密钥，取消初始化后清理，端点清理失败仍清理 Host/数据库，超时后不会在等待中的预检查完成后开始发送，关闭期间撤销未开始发送，已开始发送的不确定状态不自动重发，取消标记清理。
4. 字符串 `[CQ:...]` 保持普通文本，不解析为组件。

本版最终结果：最低 aiohttp 3.11.18 环境共发现 224 项，212 项通过、12 项浏览器测试因缺少可用浏览器环境跳过；当前 aiohttp 3.14.4 环境运行 212 项通过，浏览器测试类整体跳过一次（12 个用例未执行）。两组均已设置官方 SDK 测试模块路径，SDK 验证未跳过。Node DOM/模拟桥测试 26/26 通过。原有消息路由、持久化、签名及安全边界回归保留，新增管理 API、Tunnel 生命周期与 schema 隐藏兼容回归。原始执行日志不包含在公开备份中。

这不是完整 UI 验收通过：真实浏览器用例未执行，没有截图证明实际渲染，也没有运行真实 AstrBot 管理界面。

端点/Events 另覆盖：Bearer 鉴权优先、禁止查询参数密钥及浏览器 Origin、严格参数和 JSON/大小边界、未知收件人参数拒绝、真实本机 HTTP 签名挑战→订阅→入库→回调→退订、持久化/刷新/到期/密钥轮换、稳定事件 ID 重试、410/413 终止、失效签名拒绝、公网 DNS 校验及地址固定、拒绝重定向、关闭后不启动排队 DNS、清理异常脱敏及端口重载。

## 实际测试版本与方法

- Python 3.12.14
- 合成本机 TLS 测试工具 OpenSSL 3.5.7（仅测试）
- aiohttp 3.11.18（隔离最低版本环境）和 3.14.4（已有云端版本）
- aiocqhttp 1.4.4，真实 Event 类型；新增依赖只装入隔离测试环境
- standardwebhooks 1.0.1
- 仅供外部类型验证：Node.js v24.19.0、官方 `@modelcontextprotocol/core` 2.3.1（仓库 modelcontextprotocol/typescript-sdk）
- 云端已有 Python mcp 1.29.0 没有升级或导入；插件没有 Node 或 MCP SDK 运行时依赖。
- 主要官方核对目标为用户的 AstrBot v4.28.2，固定提交 3c7adafa1397e182d60b1016bf88759265113c8a；也比较了当时 master 的相关公开 API。4.28.2 要求 Python >=3.12、aiohttp >=3.11.18、aiocqhttp >=1.4.4。没有执行完整 AstrBot 加载或该版本真实适配器 send 函数。

SDK 验证在独立 Node 子进程运行：对真实本机 HTTP 结果调用 `JSONRPCResultResponseSchema`、`DiscoverResultSchema`、`ListToolsResultSchema`、`CallToolResultSchema`。自定义 Events 的结构按 OpenAI 官方文档测试，另做真实本机签名回调；这些不是完整官方客户端或真实 dot 互操作认证。

复现基础测试：在独立测试环境安装 requirements.txt，并为真实 Event 类型测试安装官方 aiocqhttp==1.4.4，进入插件目录，运行 `python -m unittest discover -s tests -v`。如没有设置 MCP_CORE_MODULE，唯一官方 SDK 类型验证测试会明确跳过。要运行 SDK 类型验证项，另外在隔离的 npm 测试目录安装官方 `@modelcontextprotocol/core@2.3.1`，将环境变量 MCP_CORE_MODULE 指向该包 `dist/index.mjs` 的绝对路径，再运行同一命令。不要把测试 SDK 装进 AstrBot 的 Python 环境。

## v0.3.0 新增验证

- Dashboard 原生 API 的服务器用户名/插件身份检查、CSRF、revision 防旧页面覆盖、停止等待期间配置修改、输入严格验证、秘密只写不回显、失败恢复和退出后拒绝。
- 保存先关闭旧路由；启动/停止/退出串行，延迟启动与退出竞争、取消关闭仍清理数据库/监听器、停止失败保留子进程引用，不能重叠启动。
- 原有高级 schema 键使用官方 invisible:true，保留原类型/default/secret/options；默认基础页仅四项。新增 Tunnel 字段默认关闭。
- Tunnel 21 个 mock 子进程用例：默认关闭零执行、固定官方二进制/平台/hash校验、0644源码复制到私有可执行目录、凭据引用、认证头覆盖注意事项、环境变量白名单、进程锁、超时、TERM/KILL、重复取消、异常退出、清理、状态脱敏。
- 本地主机健康/就绪与实际连接分开：connected=null、roundtrip_verified=false；界面不会用运行状态冒充真实闭环。
- 官方 v0.0.16 amd64 ZIP/hash/SPDX/license 与本地 ELF 静态验证；原二进制未执行。Sigstore bundle 已附带但签名未验证，详见 vendor manifest。没有真实 API key、Tunnel 创建或联网调用。
- 前端使用本地外部JS/CSS与 AstrBotPluginPage 桥，DOM模拟检查保存/错误/取消/重复点击/敏感字段/状态提示；真实浏览器12项没有执行。

## v0.2.2 界面与图标回归

- 所有字段标题长度不超过 10 个字符，并使用独立非空 hint；没有用 obvious_hint 误充显示/换行开关。
- 除 description、hint、labels 等显示元数据外，11 个字段的键、类型、默认值、secret 与 options 均和 v0.2.1 一致；中文下拉标题一一对应原始机器值。
- 根目录 logo.png 与用户提供的原始 PNG 字节一致：370×370，215581 字节，SHA256 为 2f7c765f0e35044d8010ea3ed4c5a2745603566932692a9b4d152cb117452d1f。
- 根据官方 4.28.2 AstrBotConfig.vue/ConfigItemRenderer.vue 核对粗体标题、标题下方灰色 hint 和 labels 映射；不是实际仪表盘渲染测试。最终换行与主题由仪表盘 CSS 控制。

## v0.2.1 已保留回归与升级核对

- 显式 all_messages 模式接受白名单群不同发送者的普通文字，无需 @；私聊仍仅限本人。
- 原始顶层/嵌套/归一化发送者、群号、自身账号、平台及消息 ID 必须一致；机器人自身消息始终拒绝。
- 重复消息不重复入库/回复，不同发送者的来源身份保持区分；读取元数据记录实际发送者。
- 收窄模式、异步预检查后收窄、发送者被修改，都阻止尚未开始的发送。
- 旧配置缺少 group_trigger_mode 时保留 owner_at；不支持引用/转发/媒体及其他目标 @ 的边界不变。
- 发布契约测试检查插件名称、固定数据路径、原配置键及类型、模式默认值；模拟既有配置/SQLitev2 数据重新打开，保留白名单、消息和回复去重键。
- 独立复核另执行了实际 v0.2.0 ZIP 内 host.py 创建合成私聊/群数据，再用新 host.py 打开，确认消息 ID 与完成回复保留。此项是模块级数据兼容检查，不是完整 AstrBot 安装器运行。
- 官方 4.28.2 源码确认同 metadata.name 的 ZIP 可原位替换并保持原 root_dir_name，配置及 plugin_data 在代码目录外；新增 UPGRADE.md 说明无需卸载。没有执行真实仪表盘升级，也没有验证在线仓库更新。

## v0.2.0 已保留回归

- 白名单群、本人身份、真实顶层 @机器人三个条件同时成立才接收；默认空群列表。
- 原始和归一化群路由/账号不一致、其他成员/其他群、文字假 @、@全体、引用/转发/媒体均拒绝。
- 原始 @ 在归一化时因成员查询失败消失，仍按已验证的原始真实 @ 判定；分段文本归一化一致性检查保留。
- 群移除、异步预检查之后移除群、发送前改路由均阻止发送；私聊不受新增范围扩展。
- 私聊和两个群使用同一原始消息 ID 不碰撞；读工具返回明确的会话/隐私范围。
- 旧数据库迁移保留消息 ID、回复键、旧事件引用，旧消息标为私聊；失败时完整回滚，错误绑定不迁移。
- 自动发现零/一/多候选、手填保留、保存失败、未知元数据、假异步 API、安全候选提示及不读取网络客户端/QQ 身份。
- 初始化只有第一个平台时不保存，完整 loaded hook 看到两个后仍留空；新安装/重载不猜测启动完成。
- 已加载一个但配置中另有启用失败的平台，仍不自动选择；公开配置 API 缺失则保留空白。
- 缺少可选 discovery hook/API 不影响已手动配置的严格启动；缺少 QQ 不因自动发现而启用；v0.3.2 内部认证由明确启动流程自动管理。

## v0.1.1 已保留回归

- 普通 dict 原始消息在接收和发送阶段均拒绝，真实 aiocqhttp.Event 才通过。
- 原始非文本/混合/Markdown/格式错误数组拒绝；原始与归一化消息 ID 必须一致。
- 原始首尾空格和字面 CQ 文本保留；归一化规则不匹配时拒绝。
- 必需公开方法缺失时启动失败；运行事件缺少发送能力时不接收。
- 异步预检查完成前禁用配置，发送仍被阻止。

## 源码核对

- https://docs.astrbot.app/dev/star/guides/simple.html
- https://docs.astrbot.app/dev/star/plugin-new.html
- https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/platform/astr_message_event.py
- https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py
- https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/star/base.py
- https://github.com/nonebot/aiocqhttp/blob/master/aiocqhttp/event.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/star_manager.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/config/astrbot_config.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/dashboard/src/components/shared/AstrBotConfig.vue
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/dashboard/src/components/shared/ConfigItemRenderer.vue
- https://docs.astrbot.app/use/plugin.html
- https://developers.openai.com/plugins/build/mcp-events

只读复核未发现此有限范围内仍未处理的阻断问题；这不是生产安全审计。

## 诚实边界

- mock 测试不是实际 AstrBot 插件加载测试。代码 API 已对照固定 v4.28.2 官方源文件；没有安装并启动完整 AstrBot，也没有执行真实适配器 send。实际依赖中的 Event 类参与测试不等于真实框架运行。
- 没有真实 QQ 私聊/群聊入站或出站、QQ 官方机器人适配器（明确不支持）、账号授权、dot 插件订阅或端到端唤醒测试。
- 本次测试未创建隧道、HTTPS 代理、外部订阅、账号或真实凭证；运行时只在用户明确启用/应用后的首次启动生成本机内部 MCP 密钥，查看页面不会生成。
- `adapter_completed` 只表示适配器调用完成，不证明发送成功。已有适配器可能吞掉错误或内部重试。
- 等待中的预检查超时/取消后不会开始发送；一旦已进入适配器网络发送，无法撤回或保证消息未被收到。第三方适配器若永久吞掉取消，终止等待可能挂起。
- 回复句柄仅在本进程保存最近 1000 条；重启后历史消息可读但不可回复。没有使用不受支持的跨适配器 UMO 主动发送推断。
- all_messages 允许白名单群任何非机器人发送者，包括未来新成员；代码不验证账号是不是本人的小号。
- 群回复的隐私字段和规则模板不是语义 DLP 或硬性模型记忆隔离。认证客户端仍可读取已接收的不同会话；须由后续自动化遵守仅当前群上下文规则。没有创建该自动化。
- 公开方法/事件形状检查只能发现可检测的 API 变化；未来版本内部语义可能变化，不保证所有每周升级自动兼容。没有版本锁定。
- 手写 HTTP 端点只实现本原型列明的 MCP 子集，不宣称完整 MCP 2.0 一致性、安全审计认证或生产就绪。

原生基础配置源码已核对：invisible 只影响渲染，保存提交完整配置，因此隐藏字段保留。但旧基础弹窗可覆盖后来在高级页修改的值，需关闭重开；本插件 revision 防护不覆盖框架原生保存入口。
