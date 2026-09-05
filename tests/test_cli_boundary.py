from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports

SRC = Path(__file__).resolve().parent.parent / "src" / "sukuna"

FORBIDDEN = {"domain", "pydantic", "claude_settings", "terminal"}

# Discovered from the filesystem (top-level src/sukuna/*.py only, not the
# domain/infrastructure/presentation/usecase subpackages) so a new
# top-level module is picked up automatically instead of drifting
# silently out of coverage.
TOP_LEVEL_FILES = sorted(p.name for p in SRC.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("filename", TOP_LEVEL_FILES)
def test_top_level_file_does_not_import_domain_or_pydantic(filename: str) -> None:
    assert_no_forbidden_imports(SRC / filename, FORBIDDEN)
