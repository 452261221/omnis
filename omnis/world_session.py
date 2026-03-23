from __future__ import annotations

import time
from typing import Any


def build_snapshot_row(ctx_hash: str, tick: int, reason: str, recent_context: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "context_hash": str(ctx_hash),
        "tick": int(tick),
        "ts": int(time.time()),
        "reason": str(reason),
        "context_tail": str(recent_context or "")[-800:],
        "state": state,
    }


def trim_snapshot_rows(rows: list[dict[str, Any]], max_size: int = 80) -> list[dict[str, Any]]:
    if len(rows) <= max_size:
        return rows
    return rows[-max_size:]

__all__ = ["build_snapshot_row", "trim_snapshot_rows"]
