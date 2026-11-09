# Engine Research Client — Engine v1 server available, integration pending

> This is the **Harness client and mock contract tests**. Independent [seealpha-engine Research API v1](https://github.com/saoxia/seealpha-engine/blob/main/docs/RESEARCH_API.md) is now implemented **opt-in, default disabled**, but Platform grant issuance, isolated Harness tool gateway and cross-service acceptance have **not** been implemented. Do not point it at a trading/production Engine.

## Trust boundary

Only a **trusted research tool gateway** may hold a short-lived Engine research capability issued on behalf of a particular Platform tenant/task. The current DeepSeek Harness worker uses an unrestricted Shell in the same Docker user context as the controller; it MUST NOT receive the research token, the gateway's environment, or a socket that grants unrestricted engine access. The `research_tools.py` dispatcher is now hosted by the **separate trusted gateway entrypoint** `gateway_api.py` (HTTP/Mock implementation; see [GATEWAY.md](GATEWAY.md)). It is **not** installed as a live SDK tool; no Engine grant is passed to the current Agent Worker.

Engine must enforce the grant independently on *every* request: tenant, task, development purpose, allowed dataset/strategy refs, operations, expiry and rate/budget; never trust unverified `tenant_id` or `purpose` strings. Do not reuse the existing all-purpose Engine HTTP bearer token. Before enabling tools for live model calls, isolate agent Shell and gateway in different security principals/containers and enforce task-specific authorization end-to-end.

## Wire protocol

These routes are implemented by the independent Engine service when a dedicated research signing key is configured on a pinned **development** tenant; they are **not enabled by default**. They intentionally live under `/research/v1` rather than generic administrative Engine endpoints.

Every request sends:
- `Authorization: Bearer <task-scoped-research-token>`
- `X-SeeAlpha-Engine-Contract-Version: 2`
- `X-SeeAlpha-Research-Contract-Version: 1`

Every **successful** Engine response must echo both version headers; a missing/mismatched header, 426, unexpected JSON, redirect, oversized response, or invalid identity fails closed. 401/403 are denied, 409 is a task conflict, and 429/502/503/504 are unavailable. Errors are sanitized, not echoed from Engine response bodies.

| Method | Research route (opt-in) | Purpose |
| --- | --- | --- |
| `GET` | `/research/v1/datasets` | Discover authorized dataset metadata; response `{"datasets":[{"id":"...",...}]}` |
| `POST` | `/research/v1/snapshots` | Request immutable Parquet subset by `dataset_id`, `start`, `end`; response includes `snapshot_id` |
| `GET` | `/research/v1/snapshots/{id}` | Read snapshot readiness, `size_bytes`, `sha256`, `format` |
| `GET` | `/research/v1/snapshots/{id}/content` | Same-origin binary streaming; no attacker-supplied URL or redirect |
| `POST` | `/research/v1/backtests` | Submit **development/simulation** backtest with strategy/data refs and idempotency key |
| `GET` | `/research/v1/backtests/{id}` | Get actual Engine status/result |
| `POST` | `/research/v1/backtests/{id}/cancel` | Request cancellation; return observed task state |

The current backtest payload uses `purpose=development`, `execution=simulation`, `strategy_ref`, `dataset_refs`, `start`, `end`, `starting_balance`, `quote_currency`, `parameters`, and `idempotency_key`. Currently `strategy_ref` must be `account:<account_id>` for a strategy already issued to that development account. The server accepts exactly one `futures_um:<symbol>:1m|5m` dataset and `parameters={}`; it uses the existing TaskCoordinator queue, not arbitrary strategy code. The Engine verifies issued-development access and idempotent request identity. More general strategies, timeframes and parameterized runs remain future work.

This client does not perform dynamic SQL, custom shell calls or arbitrary HTTP. More advanced research methods are developed by the Agent in its own isolated Python workspace against **authorized data snapshots**, not by multiplying algorithm-specific Engine APIs.

## Data transfer and provenance

`EngineResearchClient.fetch_snapshot()` receives only same-origin bytes on an allow-listed path. Before returning a file, it verifies:
- snapshot status is `ready`, format is `parquet`;
- Engine declares a bounded integer `size_bytes` and SHA-256;
- actual streamed byte length and SHA-256 match;
- the destination is a trusted caller-supplied workspace, with an atomic no-overwrite write.

The Engine implementation must include dataset version, source, time coverage, gaps/quality, interval and transformation provenance in its snapshot metadata. Binary Parquet structure/content validation, point-in-time correctness and full Catalog source authorization **are not implemented** in this client and will need separate acceptance. Untrusted Parquet parsing should remain in the sandbox.

## CLI / developer usage

The client and tool dispatcher are importable from the Harness package, but there is no user-facing CLI to query Engine and **no configured live Engine integration from Harness**. The following pseudocode illustrates intended trusted use (do not put it into the SDK worker):

```python
from pathlib import Path
from seealpha_harness.research_client import EngineResearchClient
from seealpha_harness.research_tools import ResearchTools

# token is task-scoped and resolved in a trusted gateway after Platform authorization.
with EngineResearchClient("https://engine-research.internal", scoped_token) as client:
    tools = ResearchTools(client, Path("/trusted/tenant-task-workspace"))
    catalog = tools.invoke("engine.datasets.discover", {})
```

The SDK worker does **not** receive `scoped_token`. The new `gateway_api.py` provides an independent trusted HTTP entrypoint that hosts `ResearchTools`; actual deployment as a separate security principal and Agent integration are still pending. See [GATEWAY.md](GATEWAY.md).

## Contract test coverage

`tests/test_research_client.py` uses `httpx.MockTransport` to validate discovery → snapshot creation → size/hash verified streaming → development backtest → status and cancellation. It covers unknown tools, path injection, improper parameters, expired/denied-like HTTP errors, missing contract headers, transport failure and oversized/corrupted responses. The Engine repository now implements these routes, but these Harness mock tests do **not** prove cross-repository interoperability or production safety.

## Cross-repository implementation order

1. **Implemented in Engine**: opt-in research authorization and fixed Research API routes for a pinned development tenant (Engine PR #2).
2. Have Platform issue scoped, short-lived research grants. Do not give all-purpose Engine credentials to agents.
3. Separate Agent Shell from trusted gateway; expose only named research tools and approved artifact/data transfer.
4. **Producer and consumer mock tests exist separately**; still needed: actual cross-repository service integration tests (without live credentials).
5. Only when the user chooses, perform real-machine and model inference acceptance.
