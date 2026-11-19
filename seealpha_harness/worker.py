"""One SDK Session turn per process in the resident single-user Harness container."""

import argparse
import os
from pathlib import Path

from .mcp_profile import build_platform_profile_patch, build_research_profile_patch
from .store import TaskStore, atomic_json


def execute(task_id: str, store: TaskStore, *, runtime_factory=None) -> int:
    task = store.get(task_id)
    if runtime_factory is None:
        from deepseek_harness import DeepSeekHarness
        runtime_factory = DeepSeekHarness

    store.append_event(task_id, "progress", {"stage": "runtime_starting"})
    count = 0

    def on_notification(_notification):
        nonlocal count
        count += 1
        if count % 20 == 0:
            # Never expose raw tool arguments, environment, or SDK logs.
            store.append_event(task_id, "progress", {"stage": "agent_active", "notifications": count})

    try:
        runtime_options = {}
        bridge_origin = os.environ.get("HARNESS_RESEARCH_MCP_ORIGIN")
        bridge_task = os.environ.get("HARNESS_RESEARCH_MCP_TASK_ID")
        if bridge_origin or bridge_task:
            # The trusted provisioner must set BOTH values for a separate
            # task-bound Agent container. Fail closed on mismatched identities.
            if not bridge_origin or bridge_task != task_id:
                raise ValueError("Research MCP bridge task identity mismatch")
            patch = build_research_profile_patch(
                bridge_origin, store.home(task_id) / "patches",
            )
            runtime_options["patches"] = (str(patch),)
        platform_origin = os.environ.get("SEEALPHA_PLATFORM_RESEARCH_ORIGIN")
        platform_task = os.environ.get("SEEALPHA_PLATFORM_RESEARCH_TASK_ID")
        if platform_origin or platform_task:
            if (not platform_origin or platform_task != task_id or bridge_origin
                    or bridge_task or not os.environ.get("SEEALPHA_PLATFORM_RESEARCH_TOKEN")):
                raise ValueError("Platform MCP task identity or capability mismatch")
            patch = build_platform_profile_patch(
                platform_origin, task_id, store.home(task_id) / "patches",
            )
            runtime_options["patches"] = (str(patch),)
        with runtime_factory(
            dsh_home=str(store.home(task_id)),
            cwd=str(store.workspace(task_id)),
            provider=os.environ.get("HARNESS_PROVIDER", "deepseek-official"),
            model=os.environ.get("HARNESS_MODEL", "deepseek-v4-flash"),
            profile=os.environ.get("HARNESS_PROFILE", "sdk-minimal"),
            **runtime_options,
        ) as harness:
            if task["mode"] == "resume":
                session = harness.resume_session(task_id)
            else:
                session = harness.start_session(task_id)
            prompt = task["prompt"]
            if platform_task == task_id:
                prompt = (
                    "You have task-authorized development research MCP tools "
                    "under mcp__seealpha_research__. Use list_datasets, "
                    "create_snapshot, get_snapshot, submit_backtest, "
                    "get_backtest, cancel_backtest and candidate backtests "
                    "through MCP, not direct Engine HTTP. "
                    "These tools are scoped to your development tenant, virtual "
                    "account and approved dataset. Tool submission only queues "
                    "a simulated backtest; poll get_backtest for a real outcome. "
                    "Never claim a result before it reports completion. "
                    "No production trading, Engine admin, token inspection, or "
                    "request to arbitrary service URLs is allowed. "
                    "For local Parquet data download only, use Shell command "
                    "'python -m seealpha_harness.platform_research snapshot-fetch "
                    "--id SNAPSHOT_ID', then analyze the verified file. "
                    "For a new restricted strategy, create a local strategy.py "
                    "literal CANDIDATE and register it with Shell command "
                    "'python -m seealpha_harness.platform_research candidate-create "
                    "--file strategy.py'; submit its returned candidate_id "
                    "through MCP submit_candidate_backtest. For a full Python "
                    "NautilusTrader package with seealpha-package.json and "
                    "strategy.py, register via "
                    "'python -m seealpha_harness.platform_research "
                    "python-candidate-create --package DIRECTORY'; submit its "
                    "candidate_id with MCP submit_python_candidate_backtest "
                    "only if this explicitly enabled tool is available. "
                    "Each changed package requires a new immutable candidate ID. "
                    "Never print environment variables or credentials.\\n\\n"
                    + prompt
                )
            result = session.run(prompt, on_notification=on_notification)
        response = (result.final_response or "")[:100_000]
        finish_reason = result.finish_reason
        if finish_reason != "completed":
            atomic_json(store.outcome_path(task_id), {
                "ok": False, "error": "agent_not_completed", "finish_reason": finish_reason,
            })
            return 1
        artifact = store.write_summary(task_id, task["turn"], response)
        atomic_json(store.outcome_path(task_id), {
            "ok": True, "final_response": response, "artifact": artifact,
            "finish_reason": finish_reason,
        })
        return 0
    except Exception as exc:
        # Exception messages may include keys, prompt text or private paths.
        atomic_json(store.outcome_path(task_id), {
            "ok": False, "error": "runtime_failed", "error_type": type(exc).__name__,
        })
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    root = Path(os.environ["HARNESS_STATE_DIR"]).resolve()
    raise SystemExit(execute(args.task_id, TaskStore(root)))


if __name__ == "__main__":
    main()
