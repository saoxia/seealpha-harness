# 常驻 Harness Docker 运行模式（单用户开发）

> **选定方案，不是生产多租户安全验收。** 常驻 Docker `harness` 一次启动、多次复用；创建、续接、取消任务或提交 Engine 回测**都不启动新 Docker**。本仓库当前只有单任务运行限制；并行研究队列尚未实现。

## 容器与任务生命周期

```text
SeeAlpha Platform（未来授权与审计）
  → Harness HTTP v1 / TaskManager [一个常驻 Harness Docker]
      → task A: Python Worker 进程 → workspaces/A + homes/A
      → task B: Python Worker 进程 → workspaces/B + homes/B
      → 持久化 TaskStore、SSE 事件、Artifact
  → Engine Research API [独立常驻 Engine 服务，复用回测资源]
```

- `docker compose up -d` 只负责部署或升级常驻 Harness。研究任务只调用容器内部 `subprocess.Popen([sys.executable, "-m", "seealpha_harness.worker", ...])`；`_watch` 等待退出，超时或取消时终止进程组。
- 每次任务创建独立 `workspaces/{uuid}` 与 `homes/{uuid}`。Session 和已发布摘要保持持久化；Worker 完成后容器仍接收下一任务。当前最大并发为 1，额外请求明确返回 409，而不是偷启新容器。
- 控制器重启会把未结束的旧任务标为 `interrupted`，不自动重放可能含副作用的代码。仅已 `completed` 的 Session 可明确续接。
- Engine 是另一服务的常驻生命周期，开发回测调用其 API 并复用其计算能力；不为每次回测创建 Engine Docker。此仓库的 Engine Research Client / Gateway 仍处于接口与 Mock 验证阶段，不能视为实机联调完成。
- 先前未接线的 `task_runtime.py` / `runtime_recovery.py` 每任务 Docker/网络/回收设计，以及对应 Fake Docker 测试，已从主代码移除。不要重新引入与常驻 Worker 平行的正式任务运行时。

## 安全与未来演进

- **单容器 = 共享信任域**。Worker 和控制器目前在同一个容器、同一 UID；同进程空间/文件系统可见的信息不能视为互相隔离。工作区只是逻辑区分，模型 Key 在开发 Worker 中可读。
- **禁止**把实盘交易 API、生产 Engine Token、Gateway 管理密钥或其他客户数据放入 Agent 可读取的文件、环境变量或网络。当前只使用限额测试凭据，不用于不可信多租户任务。
- `gateway_api.py` 与任务型 `mcp_bridge.py` 仍是独立可信组件的开发代码。旧 Bridge 假设每任务私有 Sidecar，和常驻模式不兼容；默认禁用。正式接入前须引入容器**外部**的可信代理、细粒度身份与授权、凭据代持和实机边界检查，绝不可在同一个自由 Shell 容器里部署高权限 Gateway 后声称安全。
- 自主策略研究可以在一个常驻容器内继续扩展假设、实验、报告、记忆和排队机制，但客户任意代码执行、多租户强隔离要另外设计安全边界（例如独立实例、VM/受控计算环境）；不以每任务 Docker 作为默认扩容方式。
