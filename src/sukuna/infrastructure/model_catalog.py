"""Anthropic models-list catalog: a persisted local cache, and the network
fetch that refreshes it. Both are "external device" I/O (network, disk) --
no business logic lives here (see `domain/service/model_validation.py` for
the membership check itself).

The cache is a disposable derived artifact, not a source of truth like
`registry.json`: a missing, unreadable, or malformed cache file is
treated as an empty cache (a cache miss to be resolved by
`fetch_model_ids()`), and writes skip `registry.py`'s `fcntl` locking.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import ModelCatalogError
from .atomic_write import atomic_write
from .registry import sukuna_state_dir

_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_PAGE_LIMIT = 1000
_MAX_PAGES = 100


def model_catalog_path() -> Path:
    return sukuna_state_dir() / "model_catalog.json"


def read_cached_model_ids(path: Path) -> list[str]:
    """Never raises: a missing file, unreadable file, malformed JSON, or a
    JSON shape that doesn't match what `write_cached_model_ids()` writes are
    all treated as an empty cache -- the caller resolves that as a cache
    miss and falls back to `fetch_model_ids()`."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    model_ids = raw.get("model_ids")
    if not isinstance(model_ids, list) or not all(
        isinstance(item, str) for item in model_ids
    ):
        return []
    return model_ids


def write_cached_model_ids(model_ids: list[str], path: Path) -> None:
    """Temp file + `fsync` + atomic `rename` via `atomic_write()`, the same
    discipline as `registry.py`/`settings.py` -- but no `fcntl` lock, since
    this cache is not a single source of truth (see module docstring)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model_ids": model_ids}
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write(path, content, prefix=".model-catalog-")


def fetch_model_ids(*, api_key: str, timeout: float = 5.0) -> list[str]:
    """`GET https://api.anthropic.com/v1/models`, paginating via
    `after_id`/`has_more` until exhausted. All-or-nothing: a failure on any
    page (network error, non-2xx, malformed JSON/response shape) fails the
    whole fetch with `ModelCatalogError` -- a partial list from earlier
    pages is never returned, so a caller can never mistake a truncated
    catalog for a complete one."""
    model_ids: list[str] = []
    after_id: str | None = None
    for _ in range(_MAX_PAGES):
        page = _fetch_page(api_key=api_key, timeout=timeout, after_id=after_id)
        model_ids.extend(page["ids"])
        if not page["has_more"]:
            return model_ids
        if page["last_id"] == after_id:
            raise ModelCatalogError(
                "model catalog response has has_more=true but last_id did not advance"
            )
        after_id = page["last_id"]
    raise ModelCatalogError(
        f"model catalog pagination exceeded {_MAX_PAGES} pages without exhausting has_more"
    )


def _fetch_page_body(*, api_key: str, timeout: float, after_id: str | None) -> bytes:
    query: dict[str, str] = {"limit": str(_PAGE_LIMIT)}
    if after_id is not None:
        query["after_id"] = after_id
    request = urllib.request.Request(
        f"{_MODELS_URL}?{urllib.parse.urlencode(query)}",
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
            return body
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ModelCatalogError(f"fetching model catalog failed: {error}") from error


def _parse_page_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise ModelCatalogError(
            f"model catalog response is not valid JSON: {error}"
        ) from error
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("data"), list)
        or not isinstance(parsed.get("has_more"), bool)
    ):
        raise ModelCatalogError("model catalog response has an unexpected shape")
    try:
        ids = [item["id"] for item in parsed["data"]]
    except (KeyError, TypeError) as error:
        raise ModelCatalogError(
            f"model catalog response has an unexpected shape: {error}"
        ) from error
    if not all(isinstance(model_id, str) for model_id in ids):
        raise ModelCatalogError(
            "model catalog response has an unexpected shape: non-string id"
        )
    has_more = parsed["has_more"]
    last_id = parsed.get("last_id")
    if has_more and not isinstance(last_id, str):
        raise ModelCatalogError(
            "model catalog response has has_more=true but no usable last_id"
        )
    return {"ids": ids, "has_more": has_more, "last_id": last_id}


def _fetch_page(
    *, api_key: str, timeout: float, after_id: str | None
) -> dict[str, Any]:
    body = _fetch_page_body(api_key=api_key, timeout=timeout, after_id=after_id)
    return _parse_page_body(body)
