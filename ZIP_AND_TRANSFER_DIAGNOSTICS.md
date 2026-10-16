> 当前v0.4.15行为以UPDATE_0_4_15.md为准：取消自动历史；下载改20秒正文空闲限制，无60秒总时限；ZIP显式raw同时返回目录。以下保留前版设计记录。

# v0.4.14 ZIP目录与传输诊断

## 看压缩包里有什么

qq_read_attachment原调用不变。识别为ZIP的文件默认只返回中央目录的TextContent：文件名、声明大小/压缩大小、方法、是否加密/目录、危险路径/可疑膨胀标记。结果明确listing_only=true、raw_returned=false，原包没有交给模型。

只读元数据，不解压、不读取成员内容、不执行。目录和文件名仍是不可信输入，不能当作指令或内容安全证明。控制字符/终端转义会规范化，绝对路径/..只标记，不用于文件系统。

硬边界：输入仍20,000,000字节；中央目录最多1MiB，最多检查4096条，单文件名1024字节，最多展示200条，总摘要64KiB。超过展示/输出限制明确truncated，不能把截断当完整清单。损坏/ZIP64/多卷等不支持形态返回固定原因，不伪报空压缩包，不自动退回大blob。

需要原包时可显式include_raw=true，同工具返回原字节资源；该可选参数需客户端认识更新后的工具元数据。默认目录路径不需要刷新才能调用。未证实平台不支持大blob，只是列目录不必传整包；本次没有重试现场8.76MB ZIP。

## 有进度的下载与停滞

插件下载默认总60秒、连接10秒、读取空闲10秒，均有界。API解析仍独立20秒，DNS沿用既有预算；总下载截止覆盖下载内DNS与网络。持续收到字节可超过旧20秒，但总60秒或10秒没有新字节仍停止，清理临时文件/预留。不自动重试、不改公网验证/代理/HTTP适用范围。

诊断增加content_length_bytes（声明值）、bytes_received（实际累计）、last_progress_gap_ms、timeout_kind。只有可证时填total/connect/read_idle；未知不猜。声明长度不是实际完整性证明。

## 传输后半段日志

message_latency的attachment_responses分别记录Main和Endpoint，以opaque message_id/read_id关联，包含UTC毫秒、单调耗时、编码字节数和固定阶段。

Main记录授权/下载调度、scope组合与JSON预算编码；Endpoint记录dispatch、json_encode、http_prepare、http_write_eof。write_eof成功只代表本地HTTP写出完成，不证明Tunnel/连接器/模型收到或解析。异常只保留固定类别，无原始异常、URL、header、文件名、正文或base64。

原生日志每次完成一条，Main/Endpoint共享最多60条/分钟；超限只抑制日志，业务与内存状态不丢。状态按既有32条消息、每类8次操作有界保留。重启/淘汰后未知不补造时间。

平台ACK后至首次工具读仍是不可观测区间。插件没有平台队列/模型内部日志，不能凭空细分或承诺消除分钟等待。

私聊HTTP文件的既有单次授权grant仍30秒有效，每次流式检查会重验；本版60秒下载总预算不延长该授权。HTTPS图片没有这个HTTP grant限制。成功传输的进度放在Main trace，Endpoint按同read_id关联；Endpoint的succeeded只指HTTP本地写出，即使正文是工具错误结果也不表示业务成功。
