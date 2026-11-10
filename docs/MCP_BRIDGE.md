# 任务级 MCP Bridge — 安全接入说明

> 当前仅为开发代码、Mock 合同与可选独立服务构建材料；**正式 Harness 采用一个常驻容器，不会每任务启动 Agent/Bridge Docker**。此桥接未接入单容器 TaskManager，没有真实模型、真实 Engine 或实机验收。

## 独立信任链路

SeeAlpha Platform（待实现授权签发） → Trusted Gateway → 每任务 MCP Bridge → Agent Runtime

- **Gateway**：可信控制方通过 `POST /internal/v1/leases` 登记 Engine Research Grant。返回一次性的 `bridge_token`，后续状态只返回非敏感元数据；网关注册表只保存 SHA-256 摘要。
- **Task MCP Bridge**：持有且只持有该研究任务的 `bridge_token`，调用 Gateway 的 `/bridge/v1/tasks/{task_id}/*`。无 Engine 管理密钥、无 Gateway 管理令牌、无 DeepSeek 模型密钥。
- **Agent**：只知道任务私有 MCP URL，通过 DeepSeek Harness 官方 `@deepseek-ai/dsh-mcp-client` 获得七项工具；仍保留 Python/Shell 自由研究能力，不能获取 Gateway/Engine 密钥。
- **Engine**：每次研究调用仍核验其短期 HMAC Grant、数据集、开发租户、策略和操作范围。研究工具不开放实盘订单或任意 HTTP。

**关键限制**：这个早期 Bridge 方案假定每任务独立 Sidecar 网络；现在选定的**常驻单容器 Harness 不满足此假定**，所以默认不启用 Bridge。单凭不同路径、工作区或同一容器内的不同进程，不能保护 Bridge Token/Engine Grant 不受自由 Shell 读取。以后应将授权工具服务置于 Agent 容器外部的可信网络和权限边界，并重新设计任务身份认证，而不是按任务启动 Docker 或直接塞入秘密。

## 已实现的入口

| 组件 | 路径或模块 |
| --- | --- |
| 网关可信控制面 | `gateway_api.py` `/internal/v1/leases` |
| 网关任务授权面 | `gateway_api.py` `/bridge/v1/tasks/{id}/tools/{name}`、快照下载 |
| MCP Sidecar | `mcp_bridge.py` `/mcp`、`/snapshots/{id}/content` |
| SDK Patch 生成 | `mcp_profile.py`，输出不含任何 Bearer 的 Cordis Patch |
| Agent Worker | `worker.py`，校验 MCP task_id 与执行任务一致后将 Patch 传入 SDK |
| 独立镜像 | `docker/mcp-bridge.Dockerfile`，无需 DeepSeek SDK |

网关通过固定 Engine Research API 路径调用数据发现、创建快照、查询快照、校验并中转快照，以及提交/查看/取消开发回测。Agent 的 MCP 工具分别名为 `discover_datasets`、`create_snapshot`、`get_snapshot`、`fetch_snapshot`、`submit_backtest`、`get_backtest`、`cancel_backtest`。

Bridge 配置来自可信任务编排器：`BRIDGE_TASK_ID`、`BRIDGE_TASK_TOKEN_FILE`（只读私有 Secret 路径，**禁止**通过 `BRIDGE_TASK_TOKEN` 环境变量传密钥）、`BRIDGE_GATEWAY_ORIGIN`、`BRIDGE_PUBLIC_ORIGIN`。Gateway 与 Engine 全局密钥绝不能注入 Agent。

Agent Worker 可选读取 `HARNESS_RESEARCH_MCP_ORIGIN`、`HARNESS_RESEARCH_MCP_TASK_ID`；两项必须一起指定且后者与实际任务 ID 相等。当前 TaskManager 不传递这些变量，`compose.yaml` 不启动 Bridge。这是尚未接入的原型配置能力；不能仅设置变量就在常驻容器内宣称实现安全研究工具接入。

`fetch_snapshot` 返回已校验快照在私有任务网络内的只读下载 URL，供 Agent 的 Python/DuckDB/Polars 读取；Agent 需用 Engine 提供的 SHA-256、大小重新校验完整文件。文件流中断不代表成功，Bridge 不将整个大数据文件嵌入模型对话。

## 已知剩余风险

- Platform 尚未实现短期研究 Grant 颁发与审批。Bridge 的匿名 MCP 入口必须由实际网络隔离保护，不能共享不同租户任务的 Sidecar。
- Gateway 对 Grant 声明做结构检查而非验签；Engine 才是 HMAC 验证方。网关撤销不会立即撤销 Engine 已签发的令牌。
- 曾有未接线的任务级 Docker 编排，现已按 [常驻容器方案](RESIDENT_RUNTIME.md) 删除。未来需在不增加每任务 Docker 的前提下实现可信授权代理、任务认证、撤销与数据访问限制，当前均未验收。
- Agent 本身当前仍可能通过 Shell 读取自己的模型 API Key；此变更只缩小研究访问令牌权限，不解决模型密钥代持。
- Engine 当前回测仅支持已下发开发策略及 1m/5m 合约数据；AI 生成代码不能自动获得执行或正式发布权限。

自动化验证包括 `tests/test_gateway.py` 与 `tests/test_mcp_bridge.py`、各组件可选镜像的 CI 构建；**CI 构建镜像不等于运行时按任务创建容器**。真实模型、权限边界和跨仓库联调未执行。

参考：[DeepSeek Harness MCP Client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)、[MCP Python SDK 2](https://py.sdk.modelcontextprotocol.io/run/asgi/)。