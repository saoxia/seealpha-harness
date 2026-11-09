# 服务与研究成果合同草案

> **状态：部分实现，仍未冻结为生产合同。** 已在本仓库实现单用户本地开发版 Harness HTTP v1（任务创建/查询/续接/取消、SSE、Markdown 成果）；以下租户授权、资源预算、Engine Research API、结构化 Artifact 与 Web 联动仍是设计草案。未完成生产安全验收。

## 1. Platform ↔ Harness：任务与会话合同

拟议通过服务端认证的 `/v1` HTTP API；浏览器不直接连接 Harness 内部地址。

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/health` | Runtime 与合同版本、健康状态 |
| POST | `/v1/tasks` | 创建研究任务（幂等键、授权上下文、工作区/资源预算） |
| GET | `/v1/tasks/{task_id}` | 状态、Session 与成果索引 |
| POST | `/v1/tasks/{task_id}/cancel` | 幂等取消，不伪造已停止 |
| GET | `/v1/tasks/{task_id}/events` | SSE 事件流，支持游标恢复和有界重放 |
| GET | `/v1/tasks/{task_id}/artifacts` | 本地原型仅列出最终 Markdown 成果 |\n| GET | `/v1/tasks/{task_id}/artifacts/{artifact_id}` | 下载单个已发布成果 |\n| POST | `/v1/tasks/{task_id}/continue` | 在成功完成的持久化 Session 上续接新一轮 Prompt |

协议使用 `X-SeeAlpha-Harness-Contract-Version: 1`；请求和响应需验证版本。不兼容时明确拒绝，不静默回退。与 Engine 的 `X-SeeAlpha-Engine-Contract-Version: 2` 相互独立。

拟议任务请求的语义如下（说明字段，不是可执行接口）：

```json
{
  "task_type": "research",
  "prompt": "分析 ETH 分钟均值回归并比较不同波动环境",
  "workspace_ref": "research-workspace-123",
  "capabilities": ["market_data:read", "backtest:development"],
  "limits": {
    "max_wall_seconds": 3600,
    "max_cpu": 2,
    "max_memory_mb": 4096
  }
}
```

**现有本地原型的实际任务请求只有 `prompt` 字段，且使用单一静态 Bearer 服务令牌，没有独立租户/授权/资源范围。** 上述 JSON 是未来合同候选，不能当作当前可用请求。实际授权主体、租户 ID 与任务范围必须由 Platform 经**可验证、短期、绑定任务的服务端授权**提供，不能信任模型生成、浏览器直传或上述 `capabilities` 字符串本身。Harness 仅接受已验证的上限内请求；资源和配额以服务端策略为准，字段尚未冻结。

### 状态与事件

推荐状态 `queued | running | awaiting_approval | completed | failed | cancelling | cancelled`，并保留异常中断/未知状态的真实证据。

事件包含 `task_id`、单调递增 `sequence`、`timestamp`、`type`、`payload`。候选类型：`message_delta`、`progress`、`tool_summary`、`artifact_ready`、`approval_requested`、`error`、`completed`；当前原型仅发送 `progress` 和 `terminal`（不发送原始通知内容）；工具原始参数、敏感环境变量不直接发送给浏览器。SSE 可按游标重连，去重不靠客户端猜测。

取消与任务超时是状态机操作，不能仅依赖 HTTP 连接断开；重复提交需幂等，任务恢复不重复发起未经确认的外部动作。

### 错误语义

401 未认证、403 越权/能力不足、404 不可见资源、409 状态冲突、422 参数无效、429 配额耗尽、503 下游不可用；公开错误不可泄露内部地址与密钥。错误码和请求字段在 v1 实施时定稿并添加合同测试。

## 2. Harness ↔ Engine：研究能力合同（拟新增）

**当前 SeeAlpha Engine 的 HTTP v2 是内部服务合同，尚不等于可交付给 AI 的细粒度研究授权。** 不允许给 Harness 通用 Engine Token 后直接调用现有全部接口。

拟议的最小研究能力：

| 能力 | 语义 |
| --- | --- |
| `datasets.discover` | 查询租户可访问的品种、时间范围、粒度、数据质量和 Catalog 版本 |
| `datasets.snapshot` | 创建可追溯、只读、租户范围限定的数据快照 |
| `backtests.submit` | 提交有资源预算的**开发/模拟**回测 |
| `backtests.get` | 获取任务状态、报告/成交/权益及可验证结果 |
| `backtests.cancel` | 尝试取消，维护 Engine 的真实终态 |

这里的名称是**能力名，不是已经存在的 HTTP endpoint**。Harness 仓库已实现对应的**拟议客户端与模拟契约测试**，约定 `/research/v1/*` 路由和 `X-SeeAlpha-Research-Contract-Version: 1`；Engine 服务端和真实租户授权**均未实现**，不得把该客户端连到生产通用 Engine API。详细请求和响应参见 [ENGINE_RESEARCH_CLIENT.md](ENGINE_RESEARCH_CLIENT.md)。

数据量小时允许查询 API；海量 Tick/Bar/期权数据优先使用具有时效/哈希/范围/权限约束的 Arrow/Parquet 快照或等效数据通道。Harness 在自身沙箱中使用 Python/DuckDB/Polars，自由研究因子和算法。Engine 自己负责回测计算及报告事实。

在 Engine 研究权限尚未实现前，Platform 可以使用既有已授权的开发回测链路作为临时适配；禁止“临时适配”偷偷开启 Engine 生产写权限或共享全部数据卷。

## 3. Harness → SeeAlpha Web：Research Artifact v1（拟议）

成果不局限于 Markdown。每个 artifact 拥有 `artifact_id`、`task_id`、`tenant_ref`、`kind`、`mime_type`、`size`、`sha256`、`created_at`、`source_refs`、`schema_version` 和权限信息。

候选 `kind`：`markdown`、`source_code`、`table`、`chart_spec`、`dataset`、`strategy_package`、`backtest_report`、`interactive_html`。枚举可版本化扩展，不限定未来研究成果类型。

- 前端负责通用渲染与统一登录、主题、导航。
- Engine 的权益/成交/风险结果须保存数据来源、参数、研究模式及版本，避免图表由自由文本推断财务事实。
- `interactive_html` 不能作为可信 React 组件注入主站：必须隔离源、受限 iframe、严格 CSP、权限复核、无主站会话与任意内网访问。
- 大型文件通过经授权的文件/对象访问方式获取；事件流只包含索引和摘要，避免把整个数据集推入会话。
- Harness 不维护正式的 SeeAlpha 产品 UI；若专用可视化成熟为稳定能力，可另行发布前端组件包。

## 4. 版本与验收

首批实现先冻结任务状态与 Session、事件重连、Artifact Manifest 三项边界，再扩展研究能力。每个合同须具备生产者/消费者测试、授权拒绝与重放场景；新协议不得默认依赖其他仓库的源码文件，必要的类型可通过独立版本化合同包或 OpenAPI 生成。

合同与系统实际状态一旦不一致，应更新草案或明确差异，不能将草案标注成已实现。