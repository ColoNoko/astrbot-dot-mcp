> 当前版本为 v0.5.6；分路配置、最新行为与限制见 [版本说明](UPDATE_0_5_6.md)、[README](README.md)。下文含历史版本记录，旧选项可能已退役；当前 Events 使用 aiohttp，消息读取不自动附带历史。

# v0.3.2 界面调整

移除手动 MCP 密钥输入、生成按钮及对应 API 操作。内置鉴权首次明确启动时自动建立，已有值复用；用户只需配置 Tunnel 的运行密钥。

新增默认关闭的 HTTP 代理区域，含独立只写用户名/密码、明确清除、换地址重新确认认证、防误发提示和 NAS 容器地址说明。

本版前端 DOM 模拟26项通过，真实浏览器12项未执行。以下为旧版历史界面记录，不代表当前仍有手动MCP密钥控件。

# v0.3.0 独立管理页

基础设置只显示启用插件、我的 QQ、群白名单、群回复模式。全部原高级键仍在 schema 中，只增加 invisible:true，避免 AstrBot 在插件初始化前删除历史密钥。

原生入口为 pages/connection/index.html，通过 v4.28.2 的 AstrBotPluginPage 桥调用认证的 Dashboard API；不在 MCP 监听端口上另开无认证管理页面。

独立页按平台/MCP/Tunnel/状态分区，中文短标签和行内说明。密钥只显示已设置状态；留空保留。保存、应用、停止、生成新本地密钥分别明确操作，生成需要页内确认。

当前只完成源码和 DOM 模拟测试。浏览器启动/访问环境限制阻止真实渲染测试，未提供真实 AstrBot 截图；颜色、布局和 iframe 桥整合仍需实际验收。

以下保留 v0.2.2 字段可读性与图标来源记录：

# v0.2.2 配置界面调整

截图中的问题是长说明放进了字段标题，左侧固定列显示省略号。本次仅调整显示文案和图标，存储键、类型、默认值、选项值与秘密字段标记均保持原样。

## 标题调整

| 配置键 | 原标题 | 新标题 |
|---|---|---|
| enabled | 显式启用：本人私聊，以及按 group_trigger_mode 规则接收的白名单群文本 | 启用插件 |
| platform_id | 平台实例 ID；完整启动后仅一个 aiocqhttp 实例时填入空白值，多实例请手填 | 平台实例 ID |
| adapter | 本原型仅支持 aiocqhttp；完整启动后可随唯一实例自动填入空白值 | 平台适配器 |
| self_id | 机器人自身账号 ID，精确匹配 | 机器人 QQ |
| sender_id | 你本人的 QQ 号：私聊和 owner_at 群模式仅允许此账号；all_messages 群模式不限制为此账号 | 我的 QQ |
| mcp_token | 用户自行设置至少 32 字符的专用 Bearer 密钥；只在界面遮罩，磁盘不加密 | MCP 密钥 |
| callback_hosts | 允许的 dot HTTPS 回调主机精确列表，不含协议、端口或路径 | 回调域名 |
| listen_host | 默认仅容器本地监听；外部转发需要用户自行配置 | 监听地址 |
| listen_port | 插件内部 MCP 端口 | 监听端口 |
| group_ids | 允许的 QQ 群号字符串列表；默认空。范围由 group_trigger_mode 决定，始终忽略机器人自身消息 | 群白名单 |
| group_trigger_mode | 群触发模式：owner_at=仅本人真实 @机器人（兼容旧版默认）；all_messages=白名单群任何非机器人账号发普通文字都触发，无需 @。引用/媒体仍不支持 | 群回复模式 |

## 说明与下拉框

详细说明移入 hint。AstrBot 4.28.2 的插件配置组件将它显示在标题下方的较小灰色文字区域，符合用户提供的参考布局。没有设置 obvious_hint：源码中该字段只添加醒目符号，不负责显示或换行。

群回复模式显示中文：
- “仅本人 @ 时回复”仍保存 owner_at。
- “群内所有普通文字”仍保存 all_messages。

已有配置值不改写，默认仍是 owner_at；不自动启用插件、不改变群白名单或网络设置。

## 图标

用户提供的 PNG 原样复制为插件根目录 logo.png，370×370，215581 字节；未重新生成、裁切、压缩或改色。源文件与打包图标 SHA256 相同：
2f7c765f0e35044d8010ea3ed4c5a2745603566932692a9b4d152cb117452d1f

## 验证边界

已核对 4.28.2 的 AstrBotConfig.vue、ConfigItemRenderer.vue 和插件 logo.png 加载约定，并测试配置契约及图标字节。没有在用户实际 AstrBot 仪表盘加载新版界面；最终换行、弹窗宽度和主题样式仍由仪表盘控制，本包不修改仪表盘 CSS。

官方源码：
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/dashboard/src/components/shared/AstrBotConfig.vue
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/dashboard/src/components/shared/ConfigItemRenderer.vue
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/star_manager.py
