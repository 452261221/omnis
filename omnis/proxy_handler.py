from __future__ import annotations

import hashlib
import json
from typing import Any


def build_health_etag(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


__all__ = ["build_health_etag"]
