"""Content-addressed LLM response cache.

Key = sha256 of the canonical JSON of everything that determines the response: model, messages,
tools, sampling params, prompt version and run index. The run index is part of the key on
purpose: run 1, 2 and 3 of an eval are independent samples, and each is replayable.

Entries are gzipped JSON at <dir>/<key[:2]>/<key>.json.gz and are committed to the repo, so
anyone can replay an eval without an API key and get identical numbers. An entry stores the
latency, token counts and cost measured on the original call; replays report those, not the
(near-zero) replay time.
"""

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def cache_key(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    params: dict[str, Any],
    prompt_version: str,
    run_index: int,
) -> str:
    material = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "params": params,
        "prompt_version": prompt_version,
        "run_index": run_index,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CachedResponse:
    key: str
    model: str
    message: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float | None


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json.gz"

    def get(self, key: str) -> CachedResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(gzip.decompress(path.read_bytes()))
            entry = CachedResponse(**data)
        except (OSError, ValueError, TypeError):
            return None  # corrupted or from an old format: treat as a miss and overwrite
        return entry if entry.key == key else None

    def put(self, entry: CachedResponse) -> None:
        path = self._path(entry.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(entry), sort_keys=True, ensure_ascii=False).encode()
        # mtime=0 keeps the gzip bytes identical for identical content (clean git diffs).
        blob = gzip.compress(payload, mtime=0)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
