# 每任务 Agent / MCP Bridge Docker 运行时（开发阶段）

> `task_runtime.py` 实现可执行的 Docker CLI 生命周期逻辑，但未接入正式 `TaskManager`、Gateway 注册或用户 Web。仅由 Fake Docker 单元测试验证命令。**没有启动真实 Docker、Agent 或 Engine。**

## 信任与网络拓扑

可信 Platform → [受控 Engine Research Grant 与 Gateway Lease] → 可信 Harness Controller → 任务级 Agent/Bridge 容器

- Controller 才能持有 Docker 管理权限；**禁止**把宿主 Docker socket、管理令牌、交易 API 或 Engine 密钥装入 Agent 或 Bridge。
- 每个任务创建独立 `--internal` Docker 网络，只加入本任务 Agent + MCP Bridge，没有宿主端口映射，也不能直接连接外网或 Engine。
- Bridge 另连接已经由可信管理员建立的 Gateway 网络；Agent **不加入** Gateway 网络。MCP URL 为任务内部 `http://research-sidecar:8782`。
- Bridge 只挂载只读任务凭据文件 `/run/bridge-token`；Agent 只挂载本任务独立的 `/state`。双方不能共享卷、环境变量或 UID。所有容器无 Linux Capabilities、非 root、只读 rootfs、限制 PIDs/CPU/内存、`no-new-privileges`。
- Agent 与 Bridge 镜像均要求不可变的 `@sha256:` 摘要；不允许 `latest` 或客户端提供的镜像地址。

## 当前代码

- `WorkloadSpec`：校验任务、租户、独立镜像、可信 Gateway 网络、Agent 专属工作区与 Bridge 凭据文件。
- `TaskWorkloads.launch(spec)`：建任务网络 → Bridge 容器 → 加入 Gateway 网络 → Agent 容器 → 启动 Bridge/Agent。出现错误时按已创建资源回滚并尝试撤销 Gateway Lease。
- `TaskWorkloads.stop(spec)`：先请求 Gateway 撤销，再执行容器与网络清理；即便凭据文件丢失仍可按任务标识清理。
- `DockerCommands` 可注入 Fake Runner 进行 CI 测试，不需要 Docker 守护进程；实际 CLI 仅由可信进程显式调用，不经 HTTP 暴露给 Agent。
- `BridgeSettings.from_env()`：读取 `BRIDGE_TASK_TOKEN_FILE` 指向的私有、只读凭据文件，不再接受 `BRIDGE_TASK_TOKEN` 明文环境变量。令牌不会出现在容器启动命令/inspectable 环境字段内。

## 可选的持久化运行记录与重启回收（开发代码）

- `runtime_recovery.py` 提供 `RuntimeJournal` 与 `DurableTaskWorkloads`：在执行任意 Docker 操作**之前**写入私有任务记录；记录只包含任务/租户 ID、阶段与时间戳，不存储 Engine Grant、Bridge Token、模型密钥、Prompt 或工作区绝对路径。
- 同一个可信控制器显式调用 `launch(spec)`、`stop(tenant_id, task_id)`。控制器重启后显式调用 `reconcile_pending()`，只扫描登记过的任务，并将遗留工作负载视为**需撤销并清理**，绝不自动续跑研究代码。
- 创建的 Agent、Bridge 和私有网络加上所有权与角色标签；回收时先核验对应标签，拒绝删除碰巧占用同名的外部资源。Gateway Lease 撤销失败、Docker daemon 不可达、标签不符时保留 `cleanup_pending`，供可信控制器重试；不伪造已清理状态。
- 持久记录是**单控制器本地机制**，不能抵御持有 Controller 文件系统权限的攻击者，也不是分布式一致性/领导者选举。它**尚未接入** `TaskManager`、Gateway 注册或 Compose；测试只用 Fake Docker，不接入真实机器。

## 尚未完成（务必完成后才能启用）

1. **可信部署器**：需要按任务准备 Agent TaskStore 文件树及数据、Secret 文件，确保 Agent mount 仅有该任务数据，且 UID `10001` 对工作区有读写权限；Bridge 的 UID `10003` 能读取权限为 `0400/0600` 的 Secret 文件，不能由 Agent 读取。当前代码要求调用方提前准备好这些目录与文件；不会代为更改宿主权限。
2. **Gateway 登记**：需要可信调用方先完成 Platform 签发、Gateway Lease 注册并获取 Bridge Token，安全写入任务 Secret 文件。任务取消、失败、超时和重启后应调用撤销并清理。当前仅支持显式启动/停止；未实现控制器重启后的持久协调与孤儿任务回收。
3. **模型请求**：`--internal` 网络没有公网出口，若要真实 DeepSeek 推理，必须提供被允许的**独立模型访问代理**，代理代持模型 Key 并对用户/任务配额限流；不能通过给 Agent 外网和原始模型 Key 规避隔离。当前不引入模型代理。
4. **数据可见性**：模型可用的只读 Parquet 仍经任务私有 MCP Bridge 取得。工作区文件保留/加密/销毁与跨租户数据审计需实现；运行时隔离和容器逃逸防护需实机测试。
5. **容器生命周期**：当前 `TaskWorkloads` 是独立显式调用的代码，未与原本运行在单容器内的 `TaskManager` 任务状态机整合。创建完成但 Controller 意外中止可能遗留网络/容器，需增加持久任务运行记录和后续对账。
6. **安全验收**：实际网络隔离、Docker/内核安全、资源限额、真实 Agent/Bridge/MCP 兼容性、Engine/Platform 联调均未执行，真实机器验收时间由用户决定。

## Mock 验证

`tests/test_task_runtime.py` 使用 Fake Docker Runner 检查参数：私有内部网络、不同 UID、只读挂载、没有宿主端口与 Docker socket、没有在 Agent 命令中出现 Bridge/Engine/Gateway 凭据、失败回滚与停机、拒绝浮动镜像/不可信路径。

原 `compose.yaml` 和原 Agent API 均保持不变。不能将本次开发称作已经开启真正多租户沙箱。