# 公开源码说明

当前源码为 **v0.5.6 发送者分路试验版**，公开仓库为 [ColoNoko/astrbot-dot-mcp](https://github.com/ColoNoko/astrbot-dot-mcp)。插件固定标识为 `astrbot_plugin_dot_private`，显示名为「ChatGPT Dot MCP助手」，原位升级时保留此标识以延续配置与数据。

本版新增按发送者 QQ 选择独立 Tunnel，空路由列表保留单路模式。接收名单与群规则仍然生效；共享本地 Bearer 仅适用于同一受信任管理者，不构成强多租户鉴权隔离。详情见 [v0.5.6 更新说明](UPDATE_0_5_6.md)。

仓库既有 `vendor/linux-amd64/` 官方 Tunnel runtime v0.0.16 及许可证、校验、SPDX、来源材料保留，没有重新下载或替换二进制；Sigstore 签名未完成验证。`dist` 内 v0.3.2 ZIP 仅为历史存档，不代表当前源码；请通过仓库源码安装更新。

公开源码不应包含真实配置、密钥、数据库、消息、附件缓存、运行日志或私人记录。测试中的账号与凭据为合成样例，不可用于部署。最新针对性测试范围见 [测试报告](TEST_REPORT.md)，不代表真实 QQ、Tunnel 或 NAS 部署已验收。

图标权利归原权利人；本仓库不声称拥有或授予图片再分发权。第三方许可见 `vendor/linux-amd64/` 与 `THIRD_PARTY_EVENTS_LICENSE.txt`。本次更新不另行授予原插件代码许可证。

使用见 [README](README.md)、[安装教程](INSTALL.md)、[帮助](HELP.md)。安装教程的外部账户设置步骤沿用既有参考，未在本次更新重新核验。
