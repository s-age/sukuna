from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports


def test_detects_forbidden_name_imported_via_from_clause(tmp_path: Path) -> None:
    source = tmp_path / "offender.py"
    source.write_text("from .infrastructure import claude_settings\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"found:.*claude_settings"):
        assert_no_forbidden_imports(source, {"claude_settings"})


def test_still_detects_forbidden_dotted_specifier(tmp_path: Path) -> None:
    source = tmp_path / "offender.py"
    source.write_text(
        "from .infrastructure.claude_settings import load_settings\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError):
        assert_no_forbidden_imports(source, {"claude_settings"})


def test_detects_forbidden_segment_in_plain_dotted_import(tmp_path: Path) -> None:
    source = tmp_path / "offender.py"
    source.write_text(
        "import sukuna.infrastructure.claude_settings\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="claude_settings"):
        assert_no_forbidden_imports(source, {"claude_settings"})


def test_allows_legal_import_of_non_forbidden_name(tmp_path: Path) -> None:
    source = tmp_path / "ok.py"
    source.write_text(
        "from .infrastructure.registry import Registry\n", encoding="utf-8"
    )
    assert_no_forbidden_imports(source, {"claude_settings", "terminal"})
