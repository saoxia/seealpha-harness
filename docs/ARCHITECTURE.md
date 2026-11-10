# seealpha-harness 架构边界

> 状态：设计基线（拟议），不是现有功能清单。确切服务接口以实现、测试和后续版本化合同为准。

## 三个独立仓库

| 仓库 | 负责 | 不负责 |
| --- | --- | --- |
| `saoxia/SeeAlpha` | Web、登录、租户、业务权限、任务发起与审计、审批、策略商店 | Agent 循环、自由研究代码执行、Engine 内部计算 |
| `saoxia/seealpha-harness` | Agent SDK 适配、会话、研究任务、Tools/Skills、隔离工作区、研究成果 | 用户身份源、交易授权、生产订单、Engine 运行事实 |
| `saoxia/seealpha-engine` | 行情/Catalog、回测/模拟、风险和交易执行、运行事实 | AI Agent、产品登录、业务审批、Web UI |

独立工程意味着独立的依赖锁定、镜像、配置和发布周期。**不要求服务在一台机器上，也不要求三方使用同一个 Python 环境**。

## 控制面 / 研究计算与数据面

- **控制面**：浏览器 → SeeAlpha Web → Platform API → Harness 的认证任务 API。Platform 绑定用户和租户，裁决哪些数据/计算/工具可以使用，记录审批及任务审计。
- **研究计算与数据面**：Harness 按已授权的研究能力 → Engine Research API（已在独立 Engine 中提供默认关闭的研究 HTTP v1），获取可用数据和质量信息，提交开发回测、读取结果；必要时通过受限数据快照直接读取 Arrow/Parquet，而不是把海量 Tick 作为聊天 JSON 中转。
- Harness 不持有 Engine 的通用内部 Bearer token，也不直接访问生产交易入口。Engine 必须自己校验租户、目的、资源范围、期限和能力；不能只信任 Harness 声称的租户 ID。
- **网关实现阶段**：已实现一个独立的 Trusted Research Gateway HTTP 入口（[设计与限制](GATEWAY.md)），可在 Mock 测试中登记短期授权、限制工具、转交数据；Platform 签发、真正独立部署与 SDK 工具接入尚未实现。不得凭此假装安全联调完成，也不允许交给 Agent 通用 Engine v2 凭据。
- 数据不可用、版本不兼容、授权失效时应明确失败；不得自动回退到生产数据卷或另一个租户的数据。

## AI 研究能力不预设上限

Harness 的任务不是固定的“写一个策略”接口。它可以探索数据、生成/测试代码、分析统计关系、比较回测、产生图表和报告。新增研究方法优先通过隔离工作区内可编程工具完成，不必为每种算法新增 Engine 端点。

Engine 的稳定接口聚焦**数据发现、可追溯的数据获取、回测任务与结构化结果**。需要更复杂量化执行能力时，通过版本化 Engine 研究/策略契约演进，而不允许任意 shell 进入 Engine 生产进程。

当前策略格式 `seealpha-package.json` 与 `publish()` 目标持仓接口属于现阶段兼容目标，不定义未来全部期权、多腿或执行算法的能力上限。扩展真实执行接口需单独审查。

## Runtime 与部署

- 首选官方 **DeepSeek Harness Python SDK** 作为 Agent Runtime；SDK 从进程内驱动 `dsh` 子进程，运行时版本/插件配置在本仓库独立管理。
- **正式部署选择：一个常驻 Harness Docker 容器**。任务调度/API 在容器内长期运行，按任务启动/结束独立 Python Worker 进程；**不为每个任务或每次回测启动新 Docker**。这个单用户原型尚未把可信控制面与不可信 Shell 强隔离，不支持未经审查的多租户代码。
- 每个研究任务有独立 workspace 与 Harness home/Session 归属；任务完成终止 Worker **进程**而不停止容器，保留批准的成果。会话恢复不等于自动恢复外部 Shell/浏览器进程。运行细节见 [RESIDENT_RUNTIME.md](RESIDENT_RUNTIME.md)。
- 任务数、CPU/内存、存储、执行时间、模型调用及外部网络均可配额化；不强制第一版实现分布式调度。

## 用户界面和研究成果

- **产品页面**：SeeAlpha Web 的 RESEARCH / AI Workspace，沿用其鉴权、主题、导航和组件。
- **交互流**：Harness 提供结构化会话事件、进度、工具摘要、可审批动作和成果引用；Platform 校验并向浏览器透传或聚合。
- **研究成果**：文本、源代码、表格、图表、策略包、回测报告，以及未来可能出现的交互式研究页面。采用带类型/版本/来源的 Artifact Manifest，而不是把所有结果压成 Markdown。
- **动态可视化**：生成的 HTML/JS 必须通过隔离来源和严格 sandbox/CSP 展示，不能获得 SeeAlpha 主页面权限、Cookie、Token 或任意同源 API。稳定的通用组件归属 SeeAlpha Web；Harness 可独立开发可移植展示内容。
- 暂时不引入 iframe 微前端作为主导航，也不维护 Harness 专属业务登录系统。

## 主要工作流

```text
用户提出研究目标
  -> SeeAlpha Web / Platform 用户与租户鉴权
  -> Platform 发起有范围与预算的 Harness 研究任务
  -> Harness Session + 隔离工作区
       -> Engine 数据发现 / 授权快照 / 开发回测
       -> 自由编写代码、分析、可视化
  -> Harness 返回事件和带来源的 Artifacts
  -> Platform 登记、审核、授权策略草稿
  -> Engine 在独立执行边界加载（开发/模拟优先）
  -> 正式发布及实盘需额外授权和人工批准
```

Harness 内部可以自主规划和多轮实验；关键资产与交易权限仍由独立信任边界把关。

## Agent ↔ Research Tools（MCP Bridge 开发阶段）

已增加 [任务绑定 MCP Bridge](MCP_BRIDGE.md) 与 SDK Patch 作为研究工具协议实验，但**当前常驻 Harness Docker 不启用这些特权工具**。原每任务 Agent/Sidecar Docker 编排已移除。若未来接入 MCP，须使用外部可信安全边界代持授权，不能让自由 Shell 直接读取 Engine/Gateway 密钥；Platform 侧授权和端到端联调仍未完成。

## 发布与故障语义

- Harness API、Engine 研究合同、Research Artifact 合同分别版本化，不强迫三仓库同步部署。
- Platform/Harness/Engine 任一不可用时保留真实任务状态，明确展示失败、未知或等待恢复；不得伪造成功、下单或回测结果。
- 当前接口草案见 [INTERFACES.md](INTERFACES.md)；威胁模型见 [SECURITY.md](SECURITY.md)。