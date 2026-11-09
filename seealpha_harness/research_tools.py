"""Allow-listed, schema-aware research tools for a future trusted tool gateway.

This dispatcher MUST NOT be instantiated inside the current unrestricted
DeepSeek SDK Shell worker: it holds an Engine research capability token.
Use it only after controller/Agent process isolation is in place.
"""

from pathlib import Path
from typing import Any

from .research_client import BacktestSpec, EngineResearchClient


# Primitive tools rather than a fixed catalog of quant algorithms.
TOOL_NAMES = (
    "engine.datasets.discover",
    "engine.snapshots.create",
    "engine.snapshots.get",
    "engine.snapshots.fetch",
    "engine.backtests.submit",
    "engine.backtests.get",
    "engine.backtests.cancel",
)


def _arguments(arguments: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("Research tool arguments must be a JSON object")
    optional = optional or set()
    if not required <= arguments.keys() or not arguments.keys() <= (required | optional):
        raise ValueError("Research tool arguments do not match the contract")
    return arguments


class ResearchTools:
    """Fixed operations on a trusted research client and a controlled workspace."""

    def __init__(self, client: EngineResearchClient, workspace: Path) -> None:
        self.client = client
        # The trusted caller chooses the workspace. Never take a path from AI args.
        self.workspace = Path(workspace)

    def invoke(self, name: str, args: dict) -> dict:
        if name == "engine.datasets.discover":
            _arguments(args, set())
            return {"datasets": self.client.discover_datasets()}
        if name == "engine.snapshots.create":
            a = _arguments(args, {"dataset_id", "start", "end"})
            return self.client.create_snapshot(a["dataset_id"], a["start"], a["end"])
        if name == "engine.snapshots.get":
            a = _arguments(args, {"snapshot_id"})
            return self.client.get_snapshot(a["snapshot_id"])
        if name == "engine.snapshots.fetch":
            a = _arguments(args, {"snapshot_id"})
            saved = self.client.fetch_snapshot(a["snapshot_id"], self.workspace)
            return {
                "snapshot_id": a["snapshot_id"], "filename": saved.name,
                "size_bytes": saved.stat().st_size,
            }
        if name == "engine.backtests.submit":
            a = _arguments(
                args,
                {
                    "strategy_ref", "dataset_refs", "start", "end",
                    "starting_balance", "quote_currency", "idempotency_key",
                },
                {"parameters"},
            )
            if not isinstance(a["dataset_refs"], list):
                raise ValueError("dataset_refs must be a JSON list")
            spec = BacktestSpec(
                strategy_ref=a["strategy_ref"],
                dataset_refs=tuple(a["dataset_refs"]),
                start=a["start"], end=a["end"],
                starting_balance=a["starting_balance"],
                quote_currency=a["quote_currency"],
                parameters=a.get("parameters", {}),
            )
            return self.client.submit_backtest(spec, a["idempotency_key"])
        if name == "engine.backtests.get":
            a = _arguments(args, {"backtest_id"})
            return self.client.get_backtest(a["backtest_id"])
        if name == "engine.backtests.cancel":
            a = _arguments(args, {"backtest_id"})
            return self.client.cancel_backtest(a["backtest_id"])
        raise ValueError("Unknown or unauthorized research tool")
