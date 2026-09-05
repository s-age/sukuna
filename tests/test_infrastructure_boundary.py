from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports

SRC = Path(__file__).resolve().parent.parent / "src" / "sukuna" / "infrastructure"

RAW_IO_FORBIDDEN = {"domain", "repository", "application", "usecase", "command", "cli"}
TYPED_FORBIDDEN = {"repository", "application", "usecase", "command", "cli"}

# infrastructure/terminal mixes RAW_IO and TYPED files in the same
# subdirectory, so the RAW_IO/TYPED split cannot be derived from directory
# structure alone (unlike domain/entity vs domain/service). Each file is
# classified explicitly here, and FILE_FORBIDDEN's key set is asserted
# below to match the glob of the actual directory so an unclassified new
# file fails loudly instead of silently going unchecked.
FILE_FORBIDDEN = {
    "terminal/iterm_backend.py": RAW_IO_FORBIDDEN,
    "terminal/tmux_backend.py": RAW_IO_FORBIDDEN,
    "terminal/resolver.py": RAW_IO_FORBIDDEN,
    "terminal/protocol.py": RAW_IO_FORBIDDEN,
    "filesystem.py": RAW_IO_FORBIDDEN,
    "terminal/iterm_script.py": RAW_IO_FORBIDDEN,
    "atomic_write.py": RAW_IO_FORBIDDEN,
    "claude_settings.py": RAW_IO_FORBIDDEN,
    "model_catalog.py": RAW_IO_FORBIDDEN,
    "node_runtime.py": RAW_IO_FORBIDDEN,
    "npm_runtime.py": RAW_IO_FORBIDDEN,
    "tui/launcher.py": RAW_IO_FORBIDDEN,
    "registry.py": TYPED_FORBIDDEN,
    "registry_shard.py": TYPED_FORBIDDEN,
    "terminal/operations.py": TYPED_FORBIDDEN,
    "settings.py": TYPED_FORBIDDEN,
    "claude_projects.py": TYPED_FORBIDDEN,
}


def _discovered_files() -> set[str]:
    return {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "__init__.py"
    }


def test_infrastructure_file_classification_covers_every_file_on_disk() -> None:
    assert set(FILE_FORBIDDEN) == _discovered_files()


@pytest.mark.parametrize("filename", sorted(FILE_FORBIDDEN))
def test_infrastructure_file_does_not_import_forbidden_layers(filename: str) -> None:
    assert_no_forbidden_imports(SRC / filename, FILE_FORBIDDEN[filename])
