# seealpha-harness

SeeAlpha 的独立 **AI 量化研发服务**。面向策略开发、数据研究、开发回测、结果分析和可视化，不负责交易执行、业务鉴权或产品主界面。

> **当前状态：初始化文档阶段。** 目前仅建立工程规范、架构/安全边界及接口草案，尚无可启动的 Harness Runtime、HTTP 服务、Docker 镜像或 Engine 联调。

## 三个项目的职责

- [saoxia/SeeAlpha](https://github.com/saoxia/SeeAlpha)：Platform。统一 Web、用户/租户、任务授权、策略审批和运营。
- [saoxia/seealpha-harness](https://github.com/saoxia/seealpha-harness)：AI 研发。Agent、Skills/Tools、隔离研究环境、研究成果及展示协议。
- [saoxia/seealpha-engine](https://github.com/saoxia/seealpha-engine)：量化内核。行情/Catalog、计算/回测、风控及交易执行。

Platform 负责**谁能做什么**；Harness 执行授权范围内的自主研究；Engine 提供受控的量化数据与计算能力。Harness 可通过**独立的研究授权接口**与 Engine 通信，大数据不必经由 Platform 的聊天 API 搬运。

## 核心设计原则

1. **不限制研究方法**：Agent 可在隔离工作区编写和运行程序、分析数据、比较策略，Engine 负责可验证的回测与交易事实。
2. **严格限制真实权限**：不给 Agent 生产交易密钥、通用 Engine Token、Docker socket、业务数据库或主站权限。研究成果需要验证、审批后才能作为策略发布。
3. **前端统一，成果可扩展**：正式 AI Workspace 属于 SeeAlpha Web；Harness 输出结构化事件与多种成果（代码、表格、图表、报告、交互式可视化）。
4. **仓库和生命周期独立**：每个项目各自发布、构建 Docker 镜像，使用版本化 API/数据合同，不跨仓库 import 实现。

## 设计文档

- [AGENTS.md](AGENTS.md)：工程开发规范与验证要求
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：三项目边界、控制/数据面、AI Workspace
- [docs/SECURITY.md](docs/SECURITY.md)：威胁模型、隔离与交易权限边界
- [docs/INTERFACES.md](docs/INTERFACES.md)：Harness API v1、Engine 研究能力、Research Artifact v1 的**草案**

## 建议实施次序（均尚未实现）

1. 固定本仓库开发规范和首个纵向能力的验收标准。
2. 接入 [DeepSeek Harness 官方 Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)，在隔离工作区跑通单个可恢复 Session。
3. 实现可独立部署的 API + Docker（任务创建/查询/事件/取消/成果），先完成安全及契约测试。
4. 通过受控数据读取与**开发**回测接口对接 Engine，构建首个策略研究闭环。
5. 让 SeeAlpha Web 在 RESEARCH 中消费 Harness 事件和成果；正式发布仍由 Platform 人工审批。

DeepSeek Harness 是预览软件，尚未完成安全审计，不能用其自带权限机制代替容器/VM 和服务端授权。参考[上游安全说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md)。

**本仓库不包含上游 DeepSeek Harness 的源码副本**；后续优先作为 SDK 使用，避免不必要的 Fork。