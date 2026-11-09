"""Typed client for the *proposed*, read-only/development Engine Research API.

No Engine bearer secret belongs in an Agent's environment or filesystem.
Instantiate this client only in a trusted, separate tool gateway after the
Platform has issued a scoped capability and Engine can validate it.
The upstream Engine does not implement these routes yet.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit

import httpx

ENGINE_CONTRACT = "2"
RESEARCH_CONTRACT = "1"
ENGINE_HEADER = "X-SeeAlpha-Engine-Contract-Version"
RESEARCH_HEADER = "X-SeeAlpha-Research-Contract-Version"
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_JSON_LIMIT = 2_000_000
_DOWNLOAD_LIMIT = 512 * 1024 * 1024


class ResearchError(Exception):
    """A sanitized research operation failure."""


class ResearchDenied(ResearchError):
    """Credential was absent, expired or insufficient for the operation."""


class ResearchIncompatible(ResearchError):
    """Version, response shape or data integrity violation."""


class ResearchUnavailable(ResearchError):
    """Engine is unreachable or has a temporary failure."""


class ResearchConflict(ResearchError):
    """An already running, cancelled or incompatible research task."""


def resource_id(value: str) -> str:
    """Validate URL path segments; never allow user-authored paths/URLs."""
    if not isinstance(value, str) or not _RESOURCE_ID.fullmatch(value) or ".." in value:
        raise ValueError("Invalid research resource identifier")
    return value


def _date_bounds(start: str, end: str) -> None:
    # Accept date-only or UTC ISO strings; Engine is authoritative about its calendar.
    from datetime import datetime

    try:
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if begin.tzinfo != finish.tzinfo or begin >= finish:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Expected an increasing, comparable ISO date/time interval") from exc


@dataclass(frozen=True)
class BacktestSpec:
    """Only development/simulation can be requested through this client."""

    strategy_ref: str
    dataset_refs: tuple[str, ...]
    start: str
    end: str
    starting_balance: str
    quote_currency: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict:
        resource_id(self.strategy_ref)
        if not self.dataset_refs or len(self.dataset_refs) > 32:
            raise ValueError("At least one and at most 32 datasets are required")
        for item in self.dataset_refs:
            resource_id(item)
        _date_bounds(self.start, self.end)
        from decimal import Decimal, InvalidOperation

        try:
            if not isinstance(self.starting_balance, str):
                raise ValueError
            balance = Decimal(self.starting_balance)
            if not balance.is_finite() or balance <= 0:
                raise ValueError
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise ValueError("Starting balance must be positive and finite") from exc
        if not isinstance(self.quote_currency, str) or not re.fullmatch(
            r"[A-Z]{3,8}", self.quote_currency
        ):
            raise ValueError("Invalid settlement currency")
        if not isinstance(self.parameters, dict):
            raise ValueError("Parameters must be an object")
        payload = {
            "purpose": "development",
            "execution": "simulation",
            "strategy_ref": self.strategy_ref,
            "dataset_refs": list(self.dataset_refs),
            "start": self.start,
            "end": self.end,
            "starting_balance": str(balance),
            "quote_currency": self.quote_currency,
            "parameters": self.parameters,
        }
        try:
            encoded = json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Backtest parameters must contain only finite JSON values") from exc
        if len(encoded.encode("utf-8")) > 64_000:
            raise ValueError("Backtest request is too large")
        return payload


class EngineResearchClient:
    """Fixed allow-listed operations, no arbitrary request or redirects."""

    def __init__(
        self,
        base_url: str,
        scoped_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_json_bytes: int = _JSON_LIMIT,
        max_snapshot_bytes: int = _DOWNLOAD_LIMIT,
    ) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username or url.password
            or url.query or url.fragment or url.path not in {"", "/"}
        ):
            raise ValueError("Engine Research URL must be a service origin")
        if not isinstance(scoped_token, str) or not scoped_token.strip():
            raise ValueError("Engine Research requires a scoped capability token")
        if max_json_bytes < 1024 or max_snapshot_bytes < 1:
            raise ValueError("Invalid response limits")
        self.max_json_bytes = max_json_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {scoped_token}",
                ENGINE_HEADER: ENGINE_CONTRACT,
                RESEARCH_HEADER: RESEARCH_CONTRACT,
                "Accept": "application/json",
            },
            transport=transport,
            timeout=httpx.Timeout(30),
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> "EngineResearchClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code == 426:
            raise ResearchIncompatible("Engine research contract version mismatch")
        if response.status_code in (401, 403):
            raise ResearchDenied("Engine rejected research authorization")
        if response.status_code in (409,):
            raise ResearchConflict("Research task state conflict")
        if response.status_code in (429, 502, 503, 504):
            raise ResearchUnavailable("Engine research service is temporarily unavailable")
        if response.status_code >= 400:
            raise ResearchError(f"Engine rejected research request (HTTP {response.status_code})")
        if response.is_redirect:
            raise ResearchIncompatible("Engine research endpoint returned a redirect")
        if (
            response.headers.get(ENGINE_HEADER) != ENGINE_CONTRACT
            or response.headers.get(RESEARCH_HEADER) != RESEARCH_CONTRACT
        ):
            raise ResearchIncompatible("Engine did not return matching contract versions")

    def _json(self, method: str, path: str, *, payload: dict | None = None) -> dict:
        try:
            with self.client.stream(method, path, json=payload) as response:
                self._check_response(response)
                raw = bytearray()
                for part in response.iter_bytes():
                    if len(raw) + len(part) > self.max_json_bytes:
                        raise ResearchIncompatible("Engine research response exceeds size limit")
                    raw.extend(part)
        except httpx.RequestError as exc:
            raise ResearchUnavailable("Engine research transport unavailable") from exc
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResearchIncompatible("Engine research response is not JSON") from exc
        if not isinstance(result, dict):
            raise ResearchIncompatible("Engine research response must be an object")
        return result

    def discover_datasets(self) -> list[dict]:
        result = self._json("GET", "/research/v1/datasets")
        datasets = result.get("datasets")
        if not isinstance(datasets, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str)
            for item in datasets
        ):
            raise ResearchIncompatible("Engine returned invalid dataset catalog")
        return datasets

    def create_snapshot(self, dataset_id: str, start: str, end: str) -> dict:
        _date_bounds(start, end)
        result = self._json(
            "POST",
            "/research/v1/snapshots",
            payload={
                "dataset_id": resource_id(dataset_id),
                "start": start,
                "end": end,
                "format": "parquet",
            },
        )
        if not isinstance(result.get("snapshot_id"), str):
            raise ResearchIncompatible("Engine returned invalid snapshot identity")
        resource_id(result["snapshot_id"])
        return result

    def get_snapshot(self, snapshot_id: str) -> dict:
        result = self._json(
            "GET", f"/research/v1/snapshots/{resource_id(snapshot_id)}"
        )
        if result.get("snapshot_id") != snapshot_id:
            raise ResearchIncompatible("Snapshot identity did not match request")
        return result

    def submit_backtest(self, spec: BacktestSpec, idempotency_key: str) -> dict:
        key = resource_id(idempotency_key)
        payload = spec.to_payload()
        # The idempotency key is also included in the authenticated request body
        # so the Engine can compare canonical request fingerprints.
        payload["idempotency_key"] = key
        result = self._json("POST", "/research/v1/backtests", payload=payload)
        if not isinstance(result.get("backtest_id"), str):
            raise ResearchIncompatible("Engine returned invalid backtest identity")
        resource_id(result["backtest_id"])
        return result

    def get_backtest(self, backtest_id: str) -> dict:
        result = self._json(
            "GET", f"/research/v1/backtests/{resource_id(backtest_id)}"
        )
        if result.get("backtest_id") != backtest_id:
            raise ResearchIncompatible("Backtest identity did not match request")
        return result

    def cancel_backtest(self, backtest_id: str) -> dict:
        result = self._json(
            "POST", f"/research/v1/backtests/{resource_id(backtest_id)}/cancel"
        )
        if result.get("backtest_id") != backtest_id:
            raise ResearchIncompatible("Cancelled backtest identity did not match request")
        return result

    def fetch_snapshot(self, snapshot_id: str, workspace: Path) -> Path:
        """Download verified Parquet into trusted workspace; no external URLs."""
        meta = self.get_snapshot(snapshot_id)
        size = meta.get("size_bytes")
        expected = meta.get("sha256")
        if meta.get("status") != "ready" or meta.get("format") != "parquet":
            raise ResearchConflict("Research snapshot is not ready as Parquet")
        if type(size) is not int or not 0 <= size <= self.max_snapshot_bytes:
            raise ResearchIncompatible("Research snapshot size is invalid or over budget")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise ResearchIncompatible("Research snapshot is missing a verified checksum")

        directory = Path(workspace)
        if directory.is_symlink():
            raise ValueError("Snapshot destination cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = directory / (resource_id(snapshot_id) + ".parquet")
        if final.exists() or final.is_symlink():
            raise FileExistsError("Snapshot already exists in workspace")
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
        total = 0
        digest = sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                try:
                    with self.client.stream(
                        "GET",
                        f"/research/v1/snapshots/{snapshot_id}/content",
                        headers={"Accept": "application/octet-stream"},
                    ) as response:
                        self._check_response(response)
                        for block in response.iter_bytes():
                            total += len(block)
                            if total > size or total > self.max_snapshot_bytes:
                                raise ResearchIncompatible("Research snapshot exceeded expected size")
                            digest.update(block)
                            output.write(block)
                except httpx.RequestError as exc:
                    raise ResearchUnavailable("Research snapshot transfer unavailable") from exc
                output.flush()
                os.fsync(output.fileno())
            if total != size or digest.hexdigest() != expected:
                raise ResearchIncompatible("Research snapshot checksum/size mismatch")
            # hardlink is atomic, and fails rather than overwriting an existing file.
            os.link(temporary, final)
            return final
        finally:
            os.unlink(temporary)
