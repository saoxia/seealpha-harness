"""Task-scoped Python/CLI research tools via the Platform proxy (no Engine tokens).

Only the fixed commands listed below are available. Intended for the resident
development Agent Shell; capability exposure is bounded to its own task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

_MAX_JSON = 1_048_576
_MAX_SNAPSHOT = 16 * 1_048_576
_COMMANDS = {
    "datasets", "snapshot_create", "snapshot_get", "snapshot_fetch",
    "backtest_submit", "backtest_get", "backtest_cancel",
}


class ResearchCommandError(RuntimeError):
    pass


class PlatformResearchClient:
    def __init__(self, origin: str, token: str, task_id: str, *, transport=None):
        url = urlsplit(origin)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.path not in {"", "/"}
            or url.query or url.fragment
        ):
            raise ValueError("Platform origin must be a trusted service origin")
        if not isinstance(token, str) or not 40 <= len(token) <= 256:
            raise ValueError("Research task capability is required")
        try:
            if str(UUID(task_id, version=4)) != task_id:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Invalid research task ID") from None
        self.task_id = task_id
        self.client = httpx.Client(
            base_url=origin.rstrip("/"), timeout=httpx.Timeout(30),
            follow_redirects=False, trust_env=False, transport=transport,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        )

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("SEEALPHA_PLATFORM_RESEARCH_ORIGIN", ""),
            os.environ.get("SEEALPHA_PLATFORM_RESEARCH_TOKEN", ""),
            os.environ.get("SEEALPHA_PLATFORM_RESEARCH_TASK_ID", ""),
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _request(self, operation: str, arguments: dict, *, binary=False):
        if operation not in _COMMANDS or not isinstance(arguments, dict):
            raise ValueError("Unknown Platform research command")
        path = f"/api/research/tasks/{self.task_id}/commands/{operation}"
        if len(json.dumps(arguments, allow_nan=False).encode()) > 16_384:
            raise ValueError("Research arguments too large")
        try:
            with self.client.stream("POST", path, json={"arguments": arguments}) as response:
                if response.is_redirect or response.status_code >= 400:
                    raise ResearchCommandError(
                        f"Platform refused research command (HTTP {response.status_code})"
                    )
                limit = _MAX_SNAPSHOT if binary else _MAX_JSON
                raw = bytearray()
                for part in response.iter_bytes():
                    if len(raw) + len(part) > limit:
                        raise ResearchCommandError("Research response too large")
                    raw.extend(part)
                if binary:
                    if response.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
                        raise ResearchCommandError("Invalid snapshot content type")
                    return bytes(raw)
        except httpx.RequestError:
            raise ResearchCommandError("Platform research service unavailable") from None
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError
            return obj
        except (ValueError, UnicodeDecodeError):
            raise ResearchCommandError("Invalid research response") from None

    def datasets(self):
        return self._request("datasets", {})

    def create_snapshot(self, start: str, end: str):
        return self._request("snapshot_create", {"start": start, "end": end})

    def get_snapshot(self, snapshot_id: str):
        return self._request("snapshot_get", {"snapshot_id": snapshot_id})

    def fetch_snapshot(self, snapshot_id: str, workspace: Path) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
            raise ValueError("Invalid snapshot identity")
        meta = self.get_snapshot(snapshot_id)
        expected = meta.get("sha256")
        size = meta.get("size_bytes")
        if (
            meta.get("snapshot_id") != snapshot_id
            or meta.get("status") != "ready" or meta.get("format") != "parquet"
            or type(size) is not int or not 0 <= size <= _MAX_SNAPSHOT
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise ResearchCommandError("Snapshot metadata is invalid")
        data = self._request("snapshot_fetch", {"snapshot_id": snapshot_id}, binary=True)
        if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
            raise ResearchCommandError("Snapshot checksum or size mismatch")
        root = Path(workspace)
        if root.is_symlink():
            raise ValueError("Research workspace may not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = root / (snapshot_id + ".parquet")
        if output.exists() or output.is_symlink():
            raise FileExistsError("Snapshot already exists")
        fd, temporary = tempfile.mkstemp(prefix=".research-", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, output)
        finally:
            os.unlink(temporary)
        return output

    def submit_backtest(self, *, start: str, end: str, starting_balance: str,
                        quote_currency: str, idempotency_key: str):
        return self._request("backtest_submit", {
            "start": start, "end": end, "starting_balance": starting_balance,
            "quote_currency": quote_currency, "idempotency_key": idempotency_key,
        })

    def get_backtest(self, backtest_id: str):
        return self._request("backtest_get", {"backtest_id": backtest_id})

    def cancel_backtest(self, backtest_id: str):
        return self._request("backtest_cancel", {"backtest_id": backtest_id})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Task-scoped SeeAlpha development research")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("datasets")
    for name in ("snapshot-create", "backtest-submit"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--start", required=True)
        cmd.add_argument("--end", required=True)
        if name == "backtest-submit":
            cmd.add_argument("--starting-balance", required=True)
            cmd.add_argument("--quote-currency", required=True)
            cmd.add_argument("--idempotency-key", required=True)
    for name in ("snapshot-get", "snapshot-fetch", "backtest-get", "backtest-cancel"):
        commands.add_parser(name).add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        with PlatformResearchClient.from_environment() as client:
            if args.command == "datasets":
                result = client.datasets()
            elif args.command == "snapshot-create":
                result = client.create_snapshot(args.start, args.end)
            elif args.command == "snapshot-get":
                result = client.get_snapshot(args.id)
            elif args.command == "snapshot-fetch":
                result = {"file": str(client.fetch_snapshot(
                    args.id, Path.cwd() / "research-data",
                ))}
            elif args.command == "backtest-submit":
                result = client.submit_backtest(
                    start=args.start, end=args.end,
                    starting_balance=args.starting_balance,
                    quote_currency=args.quote_currency,
                    idempotency_key=args.idempotency_key,
                )
            elif args.command == "backtest-get":
                result = client.get_backtest(args.id)
            else:
                result = client.cancel_backtest(args.id)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ResearchCommandError, ValueError, FileExistsError, OSError) as error:
        # Never print tokens or arbitrary remote response bodies.
        print(type(error).__name__ + ": research command failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
