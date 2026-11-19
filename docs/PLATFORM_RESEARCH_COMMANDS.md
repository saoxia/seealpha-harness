# Harness 的任务级 Platform MCP 客户端（默认关闭）

SeeAlpha 的选定路径是 **Agent MCP Client（常驻 Harness）→ Platform 任务级 MCP 代理 → Engine 原生 MCP**。不会运行独立 Gateway/MCP Bridge，也不会在 Engine 主机执行 AI 的任意 Shell。

## 使用方式

当且仅当可信 Platform 在 `POST /v1/tasks` 请求中提供合法的预绑定 `task_id` 和 `research_access` 时，TaskManager 保存只限当前任务的短期 Platform Capability，并把 `SEEALPHA_PLATFORM_RESEARCH_ORIGIN`、`SEEALPHA_PLATFORM_RESEARCH_TOKEN`、`SEEALPHA_PLATFORM_RESEARCH_TASK_ID` 传给相应的 Worker 进程，不向公共任务响应、历史列表或 SDK Prompt 输出令牌。普通任务行为不变。

已获任务权限时，Worker 动态生成 SDK 的 `@deepseek-ai/dsh-mcp-client` 配置文件，使用环境中的 Platform 任务能力作为 HTTP Authorization Bearer，连接 `POST /api/research/tasks/{task_id}/mcp`。Agent 可以直接发现并调用 `list_datasets`、`create_snapshot`、`get_snapshot`、`submit_backtest`、`get_backtest`、`cancel_backtest`、`submit_candidate_backtest`，并在额外开启可信 Python 开发回测时使用 `submit_python_candidate_backtest`。

仅候选打包登记和 Parquet 二进制下载仍需 Shell 执行 `python -m seealpha_harness.platform_research`。原固定 CLI 子命令保持兼容：

- `datasets`
- `snapshot-create --start ... --end ...`
- `snapshot-get --id ...`、`snapshot-fetch --id ...`
- `backtest-submit --start ... --end ... --starting-balance ... --quote-currency ... --idempotency-key ...`
- `backtest-get --id ...`、`backtest-cancel --id ...`

CLI 只调用 Platform `/api/research/tasks/{task_id}/commands/...`，不持有 Engine Grant 或通用管理令牌；快照下载最多 16 MiB，验证元数据、字节数与 SHA-256 并原子写入专属工作区。

研究任务不自动排队（避免短期能力在队列中超时），研究任务只能用原绑定 UUID 创建；继续一轮需要 Platform 重新提交短期令牌。旧任务重启/取消后不盲目重放。Harness API 与 Platform 在 Mock 中验证协议，不宣称真实模型与 Engine 端到端验收。

**安全限制**：任务级 Platform Capability 在自由 Shell 同 UID 的 Worker 环境可读取，因而这只是受限开发研究能力，不是对恶意 Agent 的强隔离。Platform 对任务、租户、资源、每次调用预算、状态和用途进行后端校验；Engine 仍独立 HMAC 验证。严禁注入生产账户/交易密钥、Engine 管理令牌，或通过 CLI 开放任意 HTTP/Shell 远程执行。


## 完整 AI Python Strategy（需要后端显式启用）

不再局限于 `CANDIDATE` 规则字典。AI 可以参考 SeeAlpha 的 `demo_ema_crossover`，在研究工作目录下写普通 NautilusTrader `Strategy` 子类，配套 `seealpha-package.json`（含 `format/name/version/title/entry_point/parameters`），参数至少提供 `symbol`、`allocation`、`risk_limit`。不需要在 Harness 或 Engine 中为每个新策略添加硬编码类型。

```bash
python -m seealpha_harness.platform_research python-candidate-create --package ./my-strategy
python -m seealpha_harness.platform_research candidate-get --id CANDIDATE_ID
python -m seealpha_harness.platform_research python-candidate-submit --id CANDIDATE_ID --start 2026-01-01 --end 2026-01-02 --starting-balance 1000 --quote-currency USDT --idempotency-key v1
python -m seealpha_harness.platform_research backtest-get --id BACKTEST_ID
```

`--package` 可接受策略目录或已打包的 ZIP 文件。Harness 仅上传 ZIP 字节和任务级研究令牌，不向 Engine 共享本地文件路径、容器地址或 Engine Token。Platform 根据用户真实开发租户、历史数据订阅和账户选择 Engine Docker；Engine 只负责静态检验、在**已有开发 Engine Docker 内的 Backtest Worker** 加载 Python 并运行 NautilusTrader。

该能力需要 Platform `RESEARCH_PYTHON_ENABLED=true`、对应开发 Engine `ENGINE_RESEARCH_PYTHON_ENABLED=true` 且有效研究授权，生产路径不能启用。容器隔离并非同 UID Worker 与 Engine 主进程之间的完全隔离，此功能仅面向可信开发用户，不适合直接开放任意不可信 Python 代码。AI 应检查实际完成结果，失败不能声称成功，也不自动下发到生产环境。

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

配置与 Engine 原生 MCP 开关、密钥/历史数据要求见 [SeeAlpha Platform 文档](https://github.com/saoxia/SeeAlpha/blob/main/docs/AI_RESEARCH_COMMANDS.md)。AI 生成任意 Python 策略类仍不能直接执行，后续要先建立强隔离执行环境。

## MCP 任务模式验证说明

Platform 每次 MCP 调用重新验证 Task、Turn、租户/账户/数据集授权、调用预算、撤销状态；Engine 每次使用操作级短期 Grant 验证。MCP JSON 只承载固定操作及元数据，不处理 Parquet 二进制、任意 URL、Shell 或订单。Agent 不获得 Engine 管理令牌、Engine Grant 或签名密钥。MCP 不可用时不自动回退旧 REST；生产部署前必须在隔离开发主机验证 DeepSeek SDK 与三个服务的真实协议协商、候选策略回测和取消/恢复。
