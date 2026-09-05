from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports

SRC = Path(__file__).resolve().parent.parent / "src" / "sukuna" / "usecase"

USECASE_FILES = sorted(p.name for p in SRC.glob("*.py") if p.name != "__init__.py")
assert USECASE_FILES, f"no usecase modules found under {SRC}"

FORBIDDEN = {"cli"}


@pytest.mark.parametrize("filename", USECASE_FILES)
def test_usecase_file_does_not_import_cli(filename: str) -> None:
    assert_no_forbidden_imports(SRC / filename, FORBIDDEN)
