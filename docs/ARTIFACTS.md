# 本地 Research Artifact Manifest v1（Harness 独立开发）

> 已实现的**单用户开发能力**，仅管理不可信研究草稿。没有跨仓库发布、租户隔离、策略批准或实盘授权。

## 类型与存储

- 研究任务结束时 SDK 最终回答继续保存为 `summary-1.md`、`summary-2.md` 等。旧 `GET /v1/tasks/{id}/artifacts/summary-1` 和 `id/kind/size/sha256` 保持兼容。历史摘要尚无持久 Manifest，读取时会计算基于文件元数据的 v1 记录，不能提供真正的历史防篡改承诺。
- 其余文件由 Worker 在本任务 `workspaces/{task_id}/exports/` 内创建，**不会自动被枚举公开**。仅已 `completed` 的任务可通过已认证 `POST /v1/tasks/{id}/artifacts/import` 显式导入指定单个文件，例如 `{"source_file":"exports/report.csv"}`。
- 新导入内容以独立 `artifact-<uuid>` 保存到 `artifacts/{task_id}/`，旁边有原子写入的 `.manifest.json`。不允许覆盖同名已保存制品，下载时重新核验字节大小与 SHA-256；不直接从可变 Workspace 向客户端服务文件。
- 导入格式：`.md`、`.py`、`.csv`、`.json`、`.parquet`、`.png`、`.txt`。单文件最多 16 MiB，拒绝 HTML、SVG、JS、可执行文件、目录、符号链接、硬链接、路径穿越及嵌套路径。JSON 需合法，PNG/Parquet 需文件特征符合基本签名（**不代表格式全面验证或内容无害**）。
- 所有导入内容均标为 `approval_status: unreviewed`、`provenance: untrusted_workspace`；没有自动晋级、审核通过或部署动作。工作区内生成的策略不因导入而获得执行权限。

## Manifest 示例

```json
{
  "schema_version": 1,
  "id": "artifact-0123456789abcdef0123456789abcdef",
  "artifact_id": "artifact-0123456789abcdef0123456789abcdef",
  "task_id": "<实际任务 UUID>",
  "kind": "table",
  "mime_type": "text/csv",
  "filename": "report.csv",
  "size": 16,
  "size_bytes": 16,
  "sha256": "<64 位十六进制摘要>",
  "created_at": "<UTC ISO8601>",
  "source_refs": ["workspace:exports/report.csv"],
  "provenance": "untrusted_workspace",
  "approval_status": "unreviewed"
}
```

## HTTP 接口与错误

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/v1/tasks/{id}/artifacts` | 受认证保护的 Manifest 清单 |
| POST | `/v1/tasks/{id}/artifacts/import` | 已完成任务导入，201 成功，409 未完成，422 非法路径/类型/大小 |
| GET | `/v1/tasks/{id}/artifacts/{artifact_id}` | 内容作为附件下载，开启 `nosniff`；404 不存在，409 已登记内容校验失败 |

服务使用已有 Harness Bearer + 合同版本头，绝不通过 URL 参数传 Token。大于 16 MiB 的数据快照应走未来受控的 Engine 快照通道，不塞进当前 Artifact 管理器。

## 安全限制

当前 Agent Worker、TaskManager 和成果文件在**同一个常驻 Docker、同一 UID**，所以 Agent 可能改写工作区或读取同 UID 可见数据。扩展名、路径、格式、SHA-256 和附件下载属于开发防护，不是多租户隔离。API 持有者必须**自行确认**导入文件不包含私有密钥和不应公开的数据；Manifest 不代表人工审查或沙箱证书。未来可信控制面、客户隔离和真正的审核来源应独立实现。
