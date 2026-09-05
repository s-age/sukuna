from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports

SRC = Path(__file__).resolve().parent.parent / "src" / "sukuna" / "presentation"

FORBIDDEN = {"cli", "usecase", "infrastructure", "domain", "pydantic"}

PRESENTATION_FILES = sorted(
    str(path.relative_to(SRC))
    for path in SRC.rglob("*.py")
    if path.name != "__init__.py"
)


@pytest.mark.parametrize("filename", PRESENTATION_FILES)
def test_presentation_file_does_not_import_cli_usecase_infrastructure_domain_or_pydantic(
    filename: str,
) -> None:
    assert_no_forbidden_imports(SRC / filename, FORBIDDEN)
