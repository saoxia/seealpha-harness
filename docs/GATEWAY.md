# Trusted Research Tool Gateway（开发实现）

> **实现范围**：独立网关的 HTTP 契约、任务内存授权登记、工具白名单、预算限制、受控快照转交及模拟测试。**尚未**接入 Agent SDK 或 Platform 研究授权签发；未进行真实 Engine、DeepSeek、跨仓库/实机验收。不要把本模块当作可直接开放给客户的安全沙箱。

## 安全边界

```text
Platform（用户/租户授权，未来签发任务级 Engine Grant）
   │ 独立可信控制通道（GATEWAY_CONTROL_TOKEN）
   ▼
Trusted Research Gateway（本项目的独立服务入口）
   ├── 校验调用者、任务归属、 grant 结构、工具操作与调用预算
   ├── 临时持有任务级 Engine Research Grant（只在网关内存）
   ├── 通过 EngineResearchClient 调用固定 /research/v1/* 路由
   └── 将校验后的快照缓存在独立、受控的网关工作区
                  │
                  ▼
         seealpha-engine Research API
                  │
                  ▼
             开发租户 Catalog

Agent / Shell ——（不持有控制令牌）—— 未接入网关
```

**重要：这不是把网关作为一个模块 import 到现有 `seealpha_harness.worker`。** 现有 DeepSeek SDK Agent 仍可以运行 Shell，且与原 Harness 控制器属于同一容器/UID。把网关凭据或 Engine Grant 交给该 Worker，或将网关配置和持久卷挂在同一用户可读目录，会直接破坏设计边界。

网关有独立 ASGI 入口 `seealpha_harness.gateway_api:app`；主 `seealpha_harness.api:app` 未增加网关路由。网关不接受模型 API Key、生产交易所密钥、Engine 管理令牌或 Docker socket。正式运行前应使用独立容器/VM、单独用户、环境变量、工作卷和网络策略，且仅允许可信 Platform/Controller 访问。SDK Shell 出网能力亦需限制，不能通过读取控制进程环境或共享卷绕过网关。

## 配置（网关独有）

| 变量 | 要求 |
| --- | --- |
| `GATEWAY_CONTROL_TOKEN` | 必填、至少 32 字符，**不同于** `HARNESS_API_TOKEN` |
| `ENGINE_RESEARCH_URL` | 必填、由管理员固定的 Engine 服务 origin，例如 `https://engine.internal`；请求体不能覆盖 |
| `GATEWAY_STATE_DIR` | 独立可信状态卷，默认 `/gateway-state`，不可与 Agent 的工作区共享 |
| `GATEWAY_MAX_CALLS_PER_TASK` | 服务端上限，默认 500 |
| `GATEWAY_MAX_SNAPSHOT_BYTES` | 每个快照文件上限，默认 128 MiB |

网关必须和 Engine 相互隔离，使用 Engine 研究接口的单独签发 grant，不能使用 `ENGINE_TOKEN`。当前仅在测试中模拟已签发 grant，不能靠手工填写假的令牌启动真实研究流程。

可用 `docker/gateway.Dockerfile` 独立构建网关镜像；镜像只包含 HTTP/数据客户端依赖，不安装 DeepSeek SDK，默认非 root。Docker 镜像/命令只代表部署材料，不代表沙箱安全已经验证。用户决定实机验收前，不需要运行该容器。

## HTTP 合同（仅可信控制方）

所有 `/internal/v1/*` 接口必须发送：
- `Authorization: Bearer <GATEWAY_CONTROL_TOKEN>`
- `X-SeeAlpha-Gateway-Contract-Version: 1`

| 方法 | 地址 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 不需要鉴权，不暴露密钥或引擎地址 |
| `POST` | `/internal/v1/leases` | 登记任务与租户绑定的 Engine grant、调用预算 |
| `GET` | `/internal/v1/tasks/{task_id}` | 查询剩余调用次数、已授权工具和过期时间 |
| `DELETE` | `/internal/v1/tasks/{task_id}` | 撤销网关登记，阻止后续新调用（幂等） |
| `POST` | `/internal/v1/tasks/{task_id}/tools/{tool_name}` | 按 grant 授权执行七类研究操作 |
| `GET` | `/internal/v1/tasks/{task_id}/snapshots/{snapshot_id}/content` | 仅允许可信调用方获取已成功校验、属于此任务的快照 |

`POST /leases` 请求：

```json
{
  "tenant_id": "dev-tenant",
  "task_id": "research-123",
  "engine_grant": "<由 Platform 授权签发、绑定同一租户/任务的短期 Engine Research Grant>",
  "max_calls": 100
}
```

网关不会回传 `engine_grant`，不会给任务指定任意 Engine URL、工作区路径或生产操作；只维护**进程内**登记，网关重启后全部登记失效，需可信控制方重新授权登记。工具调用请求固定为 `{"arguments":{...}}`；工具名、参数及下载内容受到白名单/限额约束。

现有 `research_tools.py` 的七种 primitive 操作：
`engine.datasets.discover`、`engine.snapshots.create`、`engine.snapshots.get`、`engine.snapshots.fetch`、`engine.backtests.submit`、`engine.backtests.get`、`engine.backtests.cancel`。不包含订单、实盘策略启停、任意 HTTP、动态 SQL 或文件路径控制。

这些是**数据与回测原语**，并不限制 AI 在独立研究沙箱中自由编写 Python、计算因子、分析数据。正式可用前，要提供带安全边界的 Agent 自定义工具桥接，不应通过 Shell 环境变量直接给 Agent 网关凭据。

## 每任务 MCP Bridge（后续阶段源码已添加）

网关为每个可信登记的任务一次性返回 `bridge_token`，后续存储其摘要，单独的 `/bridge/v1/tasks/{task_id}/*` 接口只接受该任务凭据，不接受全局管理令牌。任务型 MCP Sidecar 通过它调用工具，并向 Agent 暴露私有网络下的 MCP 和只读快照接口。详见 [MCP_BRIDGE.md](MCP_BRIDGE.md)。**Agent 不可获取 bridge_token；独立容器/网络编排仍未实现。**

## 失败和剩余风险

- 网关可对 Engine grant 的 **JSON 声明进行一致性校验**（任务、租户、用途/操作、最长 1 小时），但不掌握签名私钥/共享密钥；其校验**不等于密码学验签**。Engine 对每个实际请求独立校验 HMAC 和范围。只有持网关控制令牌的可信方才可以登记 grant。
- 当前控制令牌是静态服务级凭据，尚无 mTLS、Platform 租户会话或短期网关调用 token。网关不得对 Agent 公开，不可把此服务和 SDK Shell 放在同一信任主体内。
- 网关撤销只阻止后续本地调用；已在途的 Engine 操作可能完成，**并不吊销 Engine 的已签发令牌**。Engine 端也尚未提供即时撤销。
- 当前注册表和调用预算在内存中，无跨实例一致性；暂不支持高可用、多节点调度、多租户生产计费。
- 每次研究调用有固定结果大小上限（1 MiB）和请求大小上限（64 KiB），快照受 Engine 客户端文件大小与校验约束；长期缓存磁盘配额、文件到期清理、持久审计与完整数据版本追溯需后续实现。
- `snapshots.fetch` 只把文件写入网关受控目录，再由可信控制方下载并以**单向、只读**方式导入 Agent 沙箱。当前尚没有 Controller → Agent 文件导入机制；不可直接把可信目录挂载为可写的 Agent 工作区。
- Engine 当前只接受已下发开发账户的 1m/5m 策略回测，不接受任意生成的策略代码。Agent 自动开发策略与隔离评测需下一阶段单独完成。

## 自动化测试

`tests/test_gateway.py` 使用 `httpx.MockTransport` 模拟 Engine Research API，并测试：
未经授权的路由请求、版本不匹配、任务/租户范围、grant 过期、无效签名在 Engine 侧拒绝、调用预算、禁止未知工具、参数与路径注入、快照校验及只允许当前任务转交、独立服务与原 Harness API 路由隔离。

此类测试是**代码及合同验证**，不代替跨容器网络隔离、真实 Engine、模型推理或实机验证。
