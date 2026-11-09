# seealpha-harness

SeeAlpha 的独立 **AI 量化研发服务**，用于策略开发、数据研究、开发回测、结果分析与可视化。它不是交易执行服务，也不维护 SeeAlpha 的用户/租户体系或正式产品 Web。

> **当前阶段：单用户、本地开发原型。** HTTP v1、DeepSeek Harness Python SDK 适配、持久化 Session、单任务 Worker、取消、事件流、Markdown 成果与 Docker 已添加。尚未与 Engine / Platform 联调，**不具备多租户安全隔离或生产实盘资格**。

## 项目边界

- [saoxia/SeeAlpha](https://github.com/saoxia/SeeAlpha)：Platform，用户/租户/授权/统一 Web/策略发布审批。
- [saoxia/seealpha-engine](https://github.com/saoxia/seealpha-engine)：量化 Engine，行情、Catalog、回测、风控及交易事实。
- **本仓库**：AI Agent Runtime、代码执行和研究 Workspace；未来通过受控 Research API 与 Engine 通信。

详见 [开发规范](AGENTS.md)、[架构](docs/ARCHITECTURE.md)、[威胁模型](docs/SECURITY.md) 和[接口草案](docs/INTERFACES.md)。已增加 **Harness 侧 Engine Research Client 与受控工具调度草案**（仅 Mock 验证），详见 [Engine Research Client](docs/ENGINE_RESEARCH_CLIENT.md)。独立 [Engine Research API 服务端](https://github.com/saoxia/seealpha-engine/blob/main/docs/RESEARCH_API.md) 已合并，但真实 Platform 授权签发、Agent 工具网关隔离、跨仓库联调、复杂 Research Artifact 和 Web 接入仍未实现，不能视为已验收。

## 快速启动（开发环境）

支持 Docker（建议）；需要可用的 DeepSeek 兼容模型服务与临时开发凭据。

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
# 编辑 .env：填入随机生成的 HARNESS_API_TOKEN 和开发用 DEEPSEEK_API_KEY
docker compose build
docker compose up -d
curl http://127.0.0.1:8777/health
```

服务只映射到宿主 `127.0.0.1:8777`，容器内使用非 root 用户；`./.local/state` 持久化任务/Session/工作区。Docker 配置了只读根文件系统、权限削减、资源限额和任务超时。但**SDK 的 shell 仍能修改容器内对其可见的文件**，本原型将控制进程和 Agent 工作负载放在同一容器与用户身份中，不能将它用于其他客户或不可信多租户任务。模型 Key 目前会进入运行 Worker 的环境，可能被 Agent Shell 读取，因此只能使用限额明确、可轮换的测试密钥，不接入私有行情、交易密钥、生产网络或真实账户。上游 `sdk-minimal` 还可能向模型提供部分 Session 日志，敏感研究数据不应在此原型中处理。

## HTTP v1

所有 `/v1/*` 接口要求 `Authorization: Bearer <HARNESS_API_TOKEN>` 和 `X-SeeAlpha-Harness-Contract-Version: 1`。仅 `/health` 不要求鉴权。任务一次只运行一个，第二个并发请求返回 409。

```bash
TOKEN='使用 .env 中的 HARNESS_API_TOKEN'
BASE=http://127.0.0.1:8777
curl -sS -X POST "$BASE/v1/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-SeeAlpha-Harness-Contract-Version: 1" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"使用 Python 生成一个简单的时间序列测试并解释代码。"}'
```

响应返回 `id`。用同样的两个请求头调用：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/tasks/{id}` | 状态、最新回答、错误码 |
| `GET` | `/v1/tasks/{id}/events?after=0` | SSE 进度、终态和游标恢复 |
| `POST` | `/v1/tasks/{id}/cancel` | 尝试取消（状态可先返回 cancelling） |
| `POST` | `/v1/tasks/{id}/continue` | 已完成 Session 续接，body 为新的 `{"prompt":"..."}` |
| `GET` | `/v1/tasks/{id}/artifacts` | 已发布 Markdown 成果清单 |
| `GET` | `/v1/tasks/{id}/artifacts/summary-1` | 第一轮结果文件 |

SDK 保存 Session 在独立 `homes/{id}`，工作区在 `workspaces/{id}`。任务恢复遵守**显式续接**：进程崩溃/服务重启时将非终态标为 `interrupted`，不会自动重放 Prompt 或恢复正在执行的 Shell；目前只允许从 `completed` 状态续接。

模型活动通知仅输出**计数型进度**，不转发原始 SDK 事件、Shell 参数、密钥或工具输出。当前 Artifact 仅保存每轮最终 Markdown 回答，Agent 在工作区生成的其他文件还未实现审批导出；不能把工作目录视为可信制品仓库。

## 本地测试

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[test]'
python -m pytest -q
python -m compileall -q seealpha_harness
```

这些测试使用假的 SDK，不调用模型、不需要 API Key。GitHub Actions 另外构建镜像并验证无模型请求的 `/health`。真实 DeepSeek 推理与完整隔离验证仍需要用户在自己的开发环境中执行。

## 开发阶段：Engine Research Client（尚未连通）

新增 `seealpha_harness/research_client.py` 与 `research_tools.py`：发现可用数据、创建/校验只读 Parquet 快照、提交开发模拟回测、读取状态和取消。协议使用 `X-SeeAlpha-Engine-Contract-Version: 2` 与独立研究合同版本 `1`；仅允许固定研究路径，不转发任意 HTTP 或实盘指令。`tests/test_research_client.py` 使用假 Engine，检查版本、拒绝、数据完整性、错误脱敏和访问范围。

**重要**：独立 Engine 已有默认关闭的 Research HTTP v1 路由；但这些工具尚未接入官方 Agent SDK，也没有 Platform 授权签发及隔离的凭据代理。不能把 Engine 研究令牌注入现有不可信 Shell。详细接口与缺口见 [Engine Research Client](docs/ENGINE_RESEARCH_CLIENT.md)。本阶段不进行实机部署或真实模型调用。

## Trusted Research Gateway（代码与 Mock 合同已实现）

新增独立服务入口 `seealpha_harness.gateway_api:app` 和 [网关隔离设计](docs/GATEWAY.md)：

- **仅限可信 Platform/Controller**：`/internal/v1/*` 使用单独 `GATEWAY_CONTROL_TOKEN` 和网关合同版本 `1`。
- **任务级研究范围**：登记 Platform 已授权的短期 Engine Grant，绑定租户/任务、操作白名单和调用预算。
- **固定工具调用**：数据发现、快照创建/获取/校验及开发回测；不允许任意 Engine HTTP、实盘或用户指定的文件路径。
- **可信文件中转**：下载只保存于网关独立存储；经过完整性检查后才能由可信控制方获取，不直接暴露给 SDK Shell。
- **独立部署材料**：`docker/gateway.Dockerfile` 不安装 DeepSeek SDK，非 root 运行。只是构建材料，尚未部署或联调。

**当前不会自动启用网关**：原 `compose.yaml`、Harness API 和 Agent Worker 不会自动加载此服务，也不会收到 Engine Grant 或网关控制令牌。需要先实现真实 Platform 授权签发、控制器/Agent 的不同权限身份与网络隔离，才可接通 Agent Tools。此阶段仍只进行自动化模拟测试，不进行实际机器验收。

## 后续路线

1. 先通过本地 Docker + 临时 Key 完成端到端研究任务测试；处理镜像、Session 与运行错误。
2. 将控制器与**不可信 Agent Worker**分离成不同权限/容器或 VM，引入受控模型网关、租户与工具权限，再考虑真实用户部署。
3. 定义并实现 Engine 研究专用授权、数据快照与开发回测接口；不直接暴露现有通用 Engine Token。
4. SeeAlpha Web 通过 Platform API 接收 Harness 事件与 Artifact，完成完整策略研发闭环。

上游 SDK 参考：[DeepSeek Harness Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)。
