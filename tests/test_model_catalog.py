import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlparse

import pytest

from sukuna.errors import ModelCatalogError
from sukuna.infrastructure.model_catalog import (
    fetch_model_ids,
    model_catalog_path,
    read_cached_model_ids,
    write_cached_model_ids,
)
from sukuna.infrastructure.registry import sukuna_state_dir


def test_model_catalog_path_is_derived_from_sukuna_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert model_catalog_path() == sukuna_state_dir() / "model_catalog.json"


def test_read_cached_model_ids_returns_empty_list_when_file_is_missing(
    tmp_path: Path,
) -> None:
    assert read_cached_model_ids(tmp_path / "model_catalog.json") == []


def test_read_cached_model_ids_returns_empty_list_for_malformed_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model_catalog.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert read_cached_model_ids(path) == []


def test_read_cached_model_ids_returns_empty_list_for_an_unexpected_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model_catalog.json"
    path.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")

    assert read_cached_model_ids(path) == []


def test_write_cached_model_ids_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "model_catalog.json"

    write_cached_model_ids(["claude-sonnet-5", "claude-opus-5"], path)

    assert read_cached_model_ids(path) == ["claude-sonnet-5", "claude-opus-5"]


def test_write_cached_model_ids_fsyncs_before_the_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fake_fsync)
    path = tmp_path / "model_catalog.json"

    write_cached_model_ids(["claude-sonnet-5"], path)

    assert len(calls) == 1
    # no leftover temp file from the write
    assert [p.name for p in tmp_path.iterdir()] == ["model_catalog.json"]


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _page_payload(ids: list[str], *, has_more: bool, last_id: str | None) -> bytes:
    return json.dumps(
        {"data": [{"id": i} for i in ids], "has_more": has_more, "last_id": last_id}
    ).encode("utf-8")


def _after_id(request: urllib.request.Request) -> str | None:
    query = parse_qs(urlparse(request.full_url).query)
    values = query.get("after_id")
    return values[0] if values else None


def test_fetch_model_ids_combines_pages_and_stops_when_has_more_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(request)
        if _after_id(request) is None:
            return _FakeResponse(
                _page_payload(
                    ["claude-sonnet-5"], has_more=True, last_id="claude-sonnet-5"
                )
            )
        return _FakeResponse(
            _page_payload(["claude-opus-5"], has_more=False, last_id=None)
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ids = fetch_model_ids(api_key="sk-test")

    assert ids == ["claude-sonnet-5", "claude-opus-5"]
    assert len(calls) == 2
    assert calls[0].get_header("X-api-key") == "sk-test"
    assert calls[0].get_header("Anthropic-version") == "2023-06-01"


def test_fetch_model_ids_raises_model_catalog_error_when_the_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelCatalogError):
        fetch_model_ids(api_key="sk-test")


def test_fetch_model_ids_a_failure_on_a_later_page_fails_the_whole_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure on page 2 must fail the whole fetch, not return the first
    page's partial results, and a caller that (incorrectly) tried to cache
    whatever `fetch_model_ids()` produced must find nothing to cache --
    `write_cached_model_ids()` is monkeypatched here (not just left
    uncalled by construction) so this test still catches a future
    refactor that moved caching inside `fetch_model_ids()` itself and
    started passing it a partial list."""
    write_calls: list[list[str]] = []
    monkeypatch.setattr(
        "sukuna.infrastructure.model_catalog.write_cached_model_ids",
        lambda ids, path: write_calls.append(ids),
    )

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        if _after_id(request) is None:
            return _FakeResponse(
                _page_payload(
                    ["claude-sonnet-5"], has_more=True, last_id="claude-sonnet-5"
                )
            )
        raise urllib.error.URLError("rate limited")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelCatalogError):
        fetch_model_ids(api_key="sk-test")

    assert write_calls == []


def test_fetch_model_ids_raises_instead_of_looping_when_last_id_does_not_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(request)
        return _FakeResponse(
            _page_payload(["claude-sonnet-5"], has_more=True, last_id="claude-sonnet-5")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelCatalogError):
        fetch_model_ids(api_key="sk-test")

    assert len(calls) == 2


def test_fetch_model_ids_raises_when_last_id_alternates_without_converging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """last_id flip-flopping between two values (A -> B -> A -> B -> ...)
    always advances relative to the *immediately previous* value, so the
    non-advance check alone never fires -- only the total page-count cap
    stops this from paginating forever."""
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(request)
        next_id = "id-a" if _after_id(request) != "id-a" else "id-b"
        return _FakeResponse(
            _page_payload(["claude-sonnet-5"], has_more=True, last_id=next_id)
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelCatalogError):
        fetch_model_ids(api_key="sk-test")

    assert len(calls) == 100


@pytest.mark.parametrize("bad_id", [123, None])
def test_fetch_model_ids_raises_when_an_id_is_not_a_string(
    monkeypatch: pytest.MonkeyPatch, bad_id: object
) -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(
            json.dumps(
                {"data": [{"id": bad_id}], "has_more": False, "last_id": None}
            ).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ModelCatalogError):
        fetch_model_ids(api_key="sk-test")
