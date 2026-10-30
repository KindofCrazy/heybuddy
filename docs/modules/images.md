# 图片输入与 QQ 附件

## 职责与架构组成

`core/images.py` 定义不可变 `ImageRef`（SHA256、JPEG 类型、字节数、宽高、首帧编号）与 Host 绑定的 `ImageReader` Protocol。`TextMessage.images` 最多四项，只允许 user 角色携带。Core 不解析 QQ URL、不下载、不依赖 SQLite。`storage/images.py::SQLiteImages` 在原数据库中保存 Agent 隔离的归一化图片 BLOB；这是 Journal 的输入附件，不是新的事实库或 Memory 权威副本。

`channels/qq/images.py` 负责有限下载与图片归一化。Pillow 仅解码 JPEG、PNG、GIF、WebP；GIF/动态 WebP 只读取首帧，不能据此解释整段动画。图片转为无 EXIF 的 JPEG，透明区域使用白色背景，最长边 1536，归一化正文最多 256 KiB。原图/签名 URL 不传给模型，也不作为用户事实写入 Memory。

## 代码实际运行路径

1. `QQGateway.serve()` → `QQAdapter.accept()` → `events.parse_text()`：投影 C2C 文本与图片附件；保留图文组合，允许无文字的纯图片。图片 ARK 卡片只取 `ark_type=picture` 的 `fields.preview`；不展开引用消息或其他卡片，不解释任意平台 JSON。
2. OpenID 白名单通过后，`QQInbox.receive()` 将文本和有限 URL 描述原子写入 received 回执；Gateway 随后才推进游标。此路径不下载、不调用模型。重复/未授权消息不下载。
3. 单消费者 `claim()` 将状态提交为 processing，同时从回执清除正文和附件 URL；在内存中的当前条目保留下载描述。领取前重启可以恢复待处理附件；领取后的崩溃仍保守 uncertain，不重跑。
4. `_process()` 抢占后台维护，检查被动窗口，然后 `QQImages.prepare()`：先校验完整附件列表，再串行读取。只允许 HTTPS 的 qpic.cn/其子域以及 multimedia.nt.qq.com、multimedia.nt.qq.com.cn，拒绝非 443、自带账号/密码、片段、任意域名和 IP URL；禁止重定向、隐式代理和 QQ 鉴权头。下载默认每图最多 8 MiB，整个附件准备最多 15 秒，最多四图。依赖官方 CDN 域名和正常 TLS 校验，不是任意网址抓取服务。
5. 纯计算 `normalize_image()` 在线程中执行：原图单边最多 8192、总像素最多 16 Mi，读首帧、调整 EXIF 方向、缩放、去元数据。超时后计算线程可能继续，但不操作磁盘/数据库。所有图片成功后才在事件循环内短事务调用 `SQLiteImages.save_image()`，取消的解码线程不会事后发布文件或数据。
6. `ChatService.run(images=refs)` → 原 `Agent.run()` → `ContextBuilder.begin()`：Run 与包含图片引用的 user 消息按原事务创建；工具执行、Character、Memory、检索、压缩及权限路径继续复用。图片本身不赋予工具权限；没有第二个 Vision Agent 或额外 OCR 模型请求。
7. 正常 Provider 请求前 `estimate_request()` 在内部 JSON 开销之外为每个图片引用预留 4096 个估算 tokens。Base64 是传输编码，不逐字符当作模型文本 tokens。这个数是 Host 的保守估算，不是精确 tokenizer 或未知模型保证；实际用量仍来自 Provider usage。
8. `OpenAIProvider._input()` 用绑定 Agent 的 `ImageReader` 校验元数据、字节数和 SHA256，才在请求内转成 Base64 `input_image`；文字用 `input_text`，普通纯文本请求保持原格式。全请求最多 128 张、归一化正文最多 32 MiB，超过时明确停止，不静默丢图。Responses API 的 detail 使用 high。SDK 的 Provider 数据重放与工具 call_id 关联不变。
9. 回答仍沿原被动纯文本回复、拆条、节流和回执路径发送；不发送图片或主动消息。后续工具轮次及重启后的正常历史重放读取同一 SQLite 图片，不再依赖过期 URL。

## 异常与边界

schema 15 原子追加 image_attachments 和 qq_receipts.attachments，旧消息缺省 images=[]，不重写 Journal。数据按 Agent 与 SHA256 隔离且只插入不覆盖。附件保存与 Run 创建是两个本地事务，崩溃可能留下孤立图片；不自动重跑、不自动删除审计附件，目前没有附件清理/配额管理，数据库会随图片增长。源 URL 只在 received 阶段暂存，领取/关闭结果和启动核对都清空它；只保留归一化图片，不保存原图。

下载失败、禁用、非法格式、数量/大小超限或非图片附件给出固定提示，不调用 LLM 猜内容。过期继续停止发送，不变成主动消息。已有 Run 引用图片缺失或校验失败时停止模型请求，保留原历史；不自动外网补图。窗口/数据库错误沿既有失败语义返回，已经执行的操作不撤销。

`core.evidence.projection()` 明确标记图片内容未披露，Summary、Reflection、Learning、Episode 和 Character State 的文本证据不包含二进制、URL或虚构 OCR 引文；原用户 caption 仍按精确引文校验，视觉结论不能仅凭 assistant 回答升级为用户已确认事实。Recall 继续只检索 messages 的文本，不为图片创建另一套事实索引。模型轮次审计保存内部引用，不保存 wire Base64 或源签名 URL；Dashboard 不增加附件下载接口。

文字 emoji 直接作为文本；图片表情包按上述格式处理。语音、视频、文件和引用消息尚不支持。仅通过其他结构/域名投递的表情仍可能被拒绝，需要核对官方事件后显式扩展，不能开放任意域名。尚未运行真实收费模型或真实 QQ 图像理解 smoke，不以 Fake Provider 断言识别质量。

## 阅读与验证

安装锁定依赖后，重启 `uv run personal-agent qq`（或原有带 dashboard 的命令），用白名单账号发送一张图或“这是什么？”加图。配置包括 `AGENT_QQ_IMAGES_ENABLED=true`、`AGENT_QQ_IMAGE_MAX_COUNT=4`、`AGENT_QQ_IMAGE_DOWNLOAD_BYTES=8388608`、`AGENT_QQ_IMAGE_DOWNLOAD_SECONDS=15`。普通回复仍使用当前模型；服务商必须支持 Responses 图片输入。

`tests/test_qq_images.py` 用合成 JPEG/PNG/GIF/WebP、MockTransport 与 Fake Provider 验证 URL 边界、无鉴权泄漏、禁止跳转、字节/总超时、坏图、首帧、纯图片/图文、去重、未授权、不支持提示、连续工具调用的图片保留、跨 Agent/重启/损坏、上下文预算、角色管线、文本证据及 schema 14→15 原子回滚。命令：`uv run pytest tests/test_qq_images.py`；完整回归继续保留 conftest 禁网保护。

协议依据：[DeepSeek 图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision/)、[Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)、[QQ C2C 事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)。模型能力与真实平台投递须分别验证。
