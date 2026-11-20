# 参数对标与默认预算

核对日期：2026-11-20。本文记录生产 Settings 的选择依据与实际接线，不表示已达到其他产品的任务成功率。验证使用离线 Fake Provider 与本地 MCP，不调用付费模型。

## 对标依据

[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference) 分开定义模型窗口、自动压缩、工具输出预算和 MCP 超时，MCP 连接默认 10 秒、调用默认 60 秒。[Claude Code 环境变量](https://code.claude.com/docs/en/env-vars) 说明输出默认值和上限依模型变化；[CLI 参考](https://code.claude.com/docs/en/cli-reference) 分别提供非交互模式的轮数和费用限制，轮数默认不设上限。这些配置支持按不同层次管理资源；不存在可直接复制到所有模型、端点和产品的一套固定参数。

heybuddy 保留显式模型窗口、有界执行和现有权限边界，采用较宽的前台多轮任务默认值。16K 输出、32 轮、64 请求和 15 分钟是本项目的选择，不是声称 Codex 或 Claude Code 使用同样默认值。

## 默认值与对应关系

| 参数 | 原生产默认 | 当前生产默认 | 对标与取舍 |
| --- | --- | --- | --- |
| 模型窗口 | 必填 | 仍必填 | 类似模型窗口配置；由 Host 核实端点能力，不猜模型名 |
| 单次输出 | 4096 tokens | 16384 tokens | 增加推理和长回答空间，必须符合模型输出能力 |
| 主模型循环 | 10 轮 | 32 轮 | 容纳多步任务，保留有限上限 |
| 根模型请求 | 24 次 | 64 次 | 包含路由、压缩、Goal 评审和只读子执行 |
| 根累计预留 | 131072 | 4000000 | 保守估算单位，计入多轮重复输入与每次完整输出上限 |
| 根执行时间 | 180 秒 | 900 秒 | 允许较长交互，仍可取消；是同一根执行的共享期限 |
| 单次模型超时 | 60 秒 | 180 秒 | 是请求总等待上限，不等同于流式连接空闲超时 |
| 摘要正文 | 2048 字节 | 8192 字节 | 保存更完整的目标、证据和待办；仍是有损摘要 |
| 摘要 API 输出 | 4096 tokens | 16384 tokens | 与可见正文字节限额独立，包含推理 |
| 压缩输入 / 超时 | 16384 / 30 秒 | 65536 / 120 秒 | 压缩更长的完整交互前缀，仍受实际模型窗口约束 |
| 压缩触发水位 | 80% | 80% | 保留原策略；分母是窗口减输出与安全余量后的可用输入 |
| 单响应 / 根工具次数 | 64 / 256 | 64 / 256 | 先保留，工具次数与模型请求数独立 |
| 本地工具超时 | 10 秒 | 10 秒 | 本地短操作保持原值 |
| MCP 工具超时 | 沿用本地默认 10 秒 | 独立默认 60 秒 | 逐 Server 的 tool_timeout_seconds 可覆盖，最多 600 秒 |
| 工具上下文正文 | 超过 16 KiB 转存 | 保留 | 原 Journal 完整，模型看引用和预览，可按需分页展开 |

生产入口从 `config.load_settings()` 到 `Settings.context_budget()`、`runtime.assemble_chat()`，将单次限额传给 ContextBuilder/Agent；`HarnessRuntime.scope()` 创建每 Run 的共享 ExecutionBudget。所有根执行请求使用同一预算，子执行不能获得一份全新额度。低层 `ContextBudget` 的嵌入默认值继续保持原来的小额度，应用入口使用上表的 Settings 默认值。

## 单位与组合

`AGENT_CONTEXT_WINDOW` 和 API 输出上限是模型 token 数；输入估算目前使用内部 JSON 序列化后的 UTF-8 字节，保守按一字节一个估算单位，还会包含内部字段开销。图片有独立估算预留。这不是模型 tokenizer，不能把累计上限 4000000 宣称为真实 4M tokens，也不能把它当作费用封顶。

`ExecutionBudget.reserved_tokens` 记录累计估算预留，`input_tokens`、`output_tokens` 记录 Provider usage，缺失 usage 时 `usage_complete=false`。预留不会按实际输出退还，失败请求也保留其预留。多轮重复历史会反复计入；较大单次窗口并不保证能连续用满 32 轮。达到请求数、累计额度、时间或工具次数中的任一上限都可能停止，已完成工具不自动重跑。

可用输入上限为 `context_window - reserved_output_tokens - safety_margin`。摘要正文使用字节，`AGENT_SUMMARY_MAX_BYTES` 是推荐名称；旧 `AGENT_SUMMARY_TOKENS` 只是同一字节上限的兼容别名，同设必须相等。窗口和输出能力由实际 Provider 决定，本地组合校验通过不表示远端接受。

## 使用与兼容

新部署填写 `.env.example` 的模型、Key 和实际窗口后使用上述默认。已有环境变量和本地 `.env` 中的显式值继续优先；升级不会自动覆盖旧的 4096 输出或 131072 累计额度。要采用新设置，请由 Host 更新对应字段后重启。不要把真实配置、Key、数据库或 Memory 提交到 Git。

小窗口或低延迟聊天可以显式使用下列配方。这里以经 Host 确认支持 16K 窗口、4K 输出的端点为例，其他参数不变；它同时降低摘要、压缩输入和输出，保证组合一致。

```dotenv
AGENT_CONTEXT_WINDOW=16384
AGENT_RESERVED_OUTPUT_TOKENS=4096
AGENT_SUMMARY_MAX_BYTES=2048
AGENT_SUMMARY_OUTPUT_TOKENS=4096
AGENT_COMPACTION_INPUT_TOKENS=8192
AGENT_MAX_ROUNDS=10
AGENT_EXECUTION_MAX_REQUESTS=24
AGENT_EXECUTION_MAX_TOKENS=524288
AGENT_EXECUTION_MAX_SECONDS=180
AGENT_REQUEST_TIMEOUT_SECONDS=60
```

如果仍配置旧 `AGENT_SUMMARY_TOKENS`，需移除它或同步设为 2048。提高输出上限不要求模型写长回答，只是允许；普通聊天长度仍由对话和角色提示决定。

后台 Organization/Learning/Reflection/Proactive 的输入、输出和维护预算，以及检索候选、Memory、Skills 和工具结果的文件字节上限保持原边界。这些模块职责不同，不能按前台任务倍率统一放大。SDK 自动重试仍关闭，未知副作用不重试，模型不能管理配置或提升权限。

## 离线验证

`tests/test_config_cli.py` 验证默认组合、旧摘要别名、文件与环境覆盖、非法次数和时间；`tests/test_harness.py` 经生产装配验证超过旧 10 轮和 131072 累计额度的工具任务，以及显式小预算停止。MCP 测试启动临时 stdio 和 loopback Server，验证独立超时和无远程模型调用。运行 `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`。
