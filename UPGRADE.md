# 原位升级与配置继承

本插件固定使用名称 `astrbot_plugin_dot_private`。后续版本保持这一身份、已有配置键与数据路径，以便原位更新。无需先卸载旧版。

## 在 AstrBot 4.28.2 更新 ZIP

1. 建议先暂停插件；为一致备份可停止 AstrBot，备份以下两个位置，再启动 AstrBot：
   - `data/config/<现有插件目录名>_config.json`
   - `data/plugin_data/astrbot_plugin_dot_private/` 整个目录
   备份含密钥和聊天数据，请保密。实际宿主机路径取决于现有 Docker 挂载，不要另建或替换用户安装。
2. 打开插件页面的「已安装」或「插件市场」，点右下角「安装插件（+）」→「从文件安装」→「选择文件」，上传新 ZIP 并安装。
3. AstrBot 按新包的 metadata.name 匹配现有插件，保留原来的插件目录标识并替换代码。不要先卸载、不要改插件名称，也不要把新包安装成另一个名字。
4. 检查原来的 QQ 绑定、group_ids、密钥和监听设置仍在，再恢复使用。

配置文件在插件源码目录之外；数据也放在固定 plugin_data 目录。包内没有用户配置或数据库。v0.3.0 保留所有 v0.2.0/v0.2.1 配置键及类型，SQLitev2 数据格式不变，继续保留消息 ID、群白名单和回复去重键。

新增 group_trigger_mode 的默认值是 owner_at，旧配置缺少此字段时也按 owner_at 运行，因此升级本身不扩大群权限。需要本次确认的全员文字模式时，在插件配置里选择“群内所有普通文字”（all_messages），保留准确的 group_ids 并保存。以后升级会保留已保存的模式。

## 在线更新与回滚边界

当前 ZIP 没有绑定已发布源码仓库。现在可按上面的方式上传新包原位更新；尚未提供自动在线检查或自下载更新器。若以后需要使用 AstrBot 的仓库在线更新，需要先明确并配置插件源码仓库。本包不会自行发布仓库或下载执行未知更新。

AstrBot 的安装替换机制会暂存旧代码，并在新代码加载失败时尝试恢复代码；这不是配置、数据库和依赖的整体事务回滚。新代码可能已写过配置或数据，因此仍须单独备份。v0.1.x 到 v0.2.x 的数据库迁移说明见 README；回退到旧数据格式前须恢复对应备份。

已核对官方 4.28.2 安装器、配置加载及数据目录源码；本次没有在真实 AstrBot 仪表盘执行升级。测试覆盖发布契约和模拟配置/数据库重新打开，不能替代实际安装器验收。

官方依据：
- https://docs.astrbot.app/use/plugin.html
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/star_manager.py
- https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/config/astrbot_config.py
- https://docs.astrbot.app/dev/star/guides/plugin-config.html

## v0.3.0 管理页与配置继承

仍直接上传同名 ZIP 更新，无需卸载。全部旧 schema 键保持原类型及默认值，高级项仅添加官方 `invisible: true`；因此 AstrBot 的配置完整性检查不会先删除旧密钥。新增 Tunnel 字段默认关闭且为空。数据库 schema 仍为 v2，不迁移或重建已有数据。

原基础配置只保留启用、我的 QQ、群白名单、群回复模式。高级配置在侧栏“插件页面”中的本插件进入。新页面保存会停止旧连接，需显式“应用 / 重启”；基本启用为 false 时不会启动。首次没有完整配置也保留管理页供修正，MCP 接收保持关闭。

官方二进制只支持本次 Linux x86_64 包。包内不带用户凭证，安装不执行、不自动下载；用户启用并应用 Tunnel 后，先校验打包哈希，再复制到私有临时目录以 owner-only 执行权限运行。旧版的配置/数据备份建议继续适用；代码回滚不保证恢复配置或依赖。

注意：不要同时编辑基础配置弹窗和此页面。高级设置保存后，请关闭并重新打开基础配置弹窗；旧弹窗的完整配置快照可能覆盖新高级值。这是 AstrBot 4.28.2 原生编辑器的并发限制。

## v0.3.1 中文实例名修复

AstrBot 4.28.2 的“机器人名称”直接作为平台实例 ID，aiocqhttp 的 meta().id 取自 config.id。v0.3.0 错误地把实例 ID 限制为 ASCII，中文实例会在发现、保存和下拉列表三个环节被排除；这是插件兼容缺陷，不需要重建或重命名已有机器人。

现支持原 ASCII 字符集及可打印、无空白的 Unicode 字符，最多 128 个 Unicode 码点。保留原字符串精确匹配，不做大小写折叠、Unicode 归一化或截断；控制/隐形/代理字符仍拒绝。错误的配置 ID 不再通过去控制字符变成另一个合法绑定。

直接上传 v0.3.1 ZIP 原位更新，重新打开连接页面/刷新状态，选择已有中文实例。全部配置键、数据库及 Tunnel 二进制不变。云端合成用例已验证中文发现/保存/事件匹配；尚未访问用户 NAS 或验证其实际页面刷新结果。

官方源码：[机器人名称字段](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/config/default.py#L893-L897)、[aiocqhttp 元数据](https://github.com/AstrBotDevs/AstrBot/blob/3c7adafa1397e182d60b1016bf88759265113c8a/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L48-L53)。

## v0.3.2 代理与内部认证

直接原位上传更新。新增代理默认关闭，旧网络路径不改变；旧 mcp_token 保留为隐藏兼容键，未删掉或强制轮换。缺少内部密钥时，仅在明确启用/应用后的首次启动生成并持久化；查看配置不生成。内置 Tunnel 的 OpenAI runtime key 仍需用户本地输入。所有代理字段隐藏在独立管理页，旧数据库不变。
