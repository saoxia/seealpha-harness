# Harness 的 Platform 研究命令客户端（默认关闭）

SeeAlpha 的选定路径是 **Agent（常驻 Harness）→ Platform 任务级研究代理 → Engine Research API**。不会运行独立 Gateway/MCP Bridge，也不会在 Engine 主机执行 AI 的任意 Shell。

## 使用方式

当且仅当可信 Platform 在 `POST /v1/tasks` 请求中提供合法的预绑定 `task_id` 和 `research_access` 时，TaskManager 保存只限当前任务的短期 Platform Capability，并把 `SEEALPHA_PLATFORM_RESEARCH_ORIGIN`、`SEEALPHA_PLATFORM_RESEARCH_TOKEN`、`SEEALPHA_PLATFORM_RESEARCH_TASK_ID` 传给相应的 Worker 进程，不向公共任务响应、历史列表或 SDK Prompt 输出令牌。普通任务行为不变。

AI 可以通过 Shell 执行 `python -m seealpha_harness.platform_research` 的固定子命令：

- `datasets`
- `snapshot-create --start ... --end ...`
- `snapshot-get --id ...`、`snapshot-fetch --id ...`
- `backtest-submit --start ... --end ... --starting-balance ... --quote-currency ... --idempotency-key ...`
- `backtest-get --id ...`、`backtest-cancel --id ...`

CLI 只调用 Platform `/api/research/tasks/{task_id}/commands/...`，不持有 Engine Grant 或通用管理令牌；快照下载最多 16 MiB，验证元数据、字节数与 SHA-256 并原子写入专属工作区。

研究任务不自动排队（避免短期能力在队列中超时），研究任务只能用原绑定 UUID 创建；继续一轮需要 Platform 重新提交短期令牌。旧任务重启/取消后不盲目重放。Harness API 与 Platform 在 Mock 中验证协议，不宣称真实模型与 Engine 端到端验收。

**安全限制**：任务级 Platform Capability 在自由 Shell 同 UID 的 Worker 环境可读取，因而这只是受限开发研究能力，不是对恶意 Agent 的强隔离。Platform 对任务、租户、资源、每次调用预算、状态和用途进行后端校验；Engine 仍独立 HMAC 验证。严禁注入生产账户/交易密钥、Engine 管理令牌，或通过 CLI 开放任意 HTTP/Shell 远程执行。

配置与 Engine 密钥/历史数据要求见 [SeeAlpha Platform 文档](https://github.com/saoxia/SeeAlpha/blob/main/docs/AI_RESEARCH_COMMANDS.md)。AI 生成任意新策略代码仍不能直接回测，需要独立增加候选代码隔离加载。
