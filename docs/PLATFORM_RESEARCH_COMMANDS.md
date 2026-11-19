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


## AI 候选版本（受限代码，不执行任意 Python）

创建文件 `candidate.py`，内容必须**只有** `CANDIDATE = {...}` 一条 Python 字面量赋值。示例：

```python
CANDIDATE = {
    "kind": "ema_cross",
    "symbol": "ETHUSDT",
    "fast_period": 8,
    "slow_period": 21,
    "allocation": "1",
    "risk_limit": "1",
}
```

AI 可修改 `kind`（`ema_cross/ema_revert/sma_cross/sma_revert`）、周期和额度；不能加入 `import`、函数、类、任意系统调用或第三方依赖。然后通过 CLI 进行候选创建及开发模拟回测：

```bash
python -m seealpha_harness.platform_research candidate-create --file candidate.py --name research_ema --version 1
python -m seealpha_harness.platform_research candidate-get --id CANDIDATE_ID
python -m seealpha_harness.platform_research candidate-submit --id CANDIDATE_ID --start 2026-01-01 --end 2026-01-02 --starting-balance 1000 --quote-currency USDT --idempotency-key iteration-1
python -m seealpha_harness.platform_research backtest-get --id BACKTEST_ID
```

`candidate-create` 自动封装包含 `strategy.py` 和 `seealpha-package.json` 的受限 ZIP；Platform 持久化原始包及 SHA-256，Engine 再静态验证，使用自身可信策略类回测。失败与未完成不可描述为回测成功。每次修改建立新候选版本，不覆盖历史。**这只是 AI 策略规则/参数实验闭环的第一阶段，不是任意生成 Python 的安全沙箱。**

配置与 Engine 密钥/历史数据要求见 [SeeAlpha Platform 文档](https://github.com/saoxia/SeeAlpha/blob/main/docs/AI_RESEARCH_COMMANDS.md)。AI 生成任意 Python 策略类仍不能直接执行，后续要先建立强隔离执行环境。
