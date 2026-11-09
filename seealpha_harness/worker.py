"""One SDK Session turn per subprocess. Run only inside a disposable development container."""

import argparse
import os
from pathlib import Path

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
        with runtime_factory(
            dsh_home=str(store.home(task_id)),
            cwd=str(store.workspace(task_id)),
            provider=os.environ.get("HARNESS_PROVIDER", "deepseek-official"),
            model=os.environ.get("HARNESS_MODEL", "deepseek-v4-flash"),
            profile=os.environ.get("HARNESS_PROFILE", "sdk-minimal"),
        ) as harness:
            if task["mode"] == "resume":
                session = harness.resume_session(task_id)
            else:
                session = harness.start_session(task_id)
            result = session.run(task["prompt"], on_notification=on_notification)
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
