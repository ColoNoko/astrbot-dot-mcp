# 图片与文件回复

v0.4.13新增第7个工具qq_reply_attachment，参数为message_id、reply_key、kind(image/file)、filename、安全原字节的data_base64。只向该条已捕获且当前仍授权的原私聊/群发送；不接受收件人、文件路径或URL。旧qq_reply仍发送文字。

由当次用户请求决定具体发送内容，不因历史/附件里的指令擅自分享文件。群回复不可夹带私聊/其他群资料。刷新工具后在新会话确认7项；既有dot持久会话不保证立即拥有新工具。

- 每个原件最多20,000,000字节。严格标准base64，无前缀/空格/非法padding。图片仅识别PNG/JPEG/GIF/WebP签名，不解码、不生成缩略图；文件原样发送，不执行、不解压。
- filename只显示安全basename，不作为读取路径。插件不创建临时文件或公开下载URL。
- 主人/群/平台/bot即时重验，然后已持久claim的reply_key才调用一次适配器。相同key同内容返回既有状态；改变kind/filename/字节冲突。不确定状态不可换key盲重发。
- adapter_completed表示兼容适配器返回有效message_id，不能证明QQ客户端送达；无回执/异常/发送后状态写入失败均uncertain。接收方后端可能有自己的处理/重试和临时文件，插件不能保证其清理。
- 文件base64消息段已按NapCat官方源码核对私聊和群支持。其他OneBot后端不保证支持；不支持时不会改用任意本地路径/临时URL。实机发送尚未验证，本次只用fakebot/loopback合成测试。
- 插件认证HTTP仅允许此工具超过原64KiB请求，最大32MiB，最多两个大请求同时解析/发送。原件20MB需要约26.67MB base64参数，模型生成大参数不是高效上传捷径，托管工具参数限制也未端到端验证；本版未增加上传服务或分块工具。

官方来源：
- https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/SendPrivateMsg.ts
- https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/SendGroupMsg.ts
- https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/api/msg.ts
- https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-common/src/file.ts

大附件资源：出站两个大请求槽与入站两个读取响应槽独立。如果同时收取并回传最大文件，base64、JSON、适配器序列化副本可叠加，建议至少约1 GiB额外可用内存；这仍不是整个进程严格RSS上限。低内存NAS请避免多份最大附件同时收发。工具接收返回小型状态不意味着请求体与适配器缓冲没有内存开销。
