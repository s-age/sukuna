from pathlib import Path

import pytest
from _boundary_support import assert_no_forbidden_imports

SRC = Path(__file__).resolve().parent.parent / "src" / "sukuna" / "domain"


def _py_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("*.py") if p.name != "__init__.py")


# Discovered from the filesystem so newly added domain/entity files are
# picked up automatically instead of drifting silently out of coverage.
ENTITY_FILES = _py_files(SRC / "entity")

# domain/mapper is allowed pydantic, so its FORBIDDEN set is
# otherwise identical to domain/entity's — pydantic was never in the
# original FORBIDDEN set to begin with.
MAPPER_FILES = _py_files(SRC / "mapper")

# domain/service may import domain/entity and domain/mapper's pure
# validators (e.g. command_mapper.validate_name/validate_role); pydantic
# (allowed only in domain/mapper) joins the forbidden set here (unlike
# domain/entity and domain/mapper).
SERVICE_FILES = _py_files(SRC / "service")

ENTITY_FORBIDDEN = {
    "json",
    "fcntl",
    "subprocess",
    "tempfile",
    "repository",
    "terminal",
    "application",
    "command",
    "cli",
    "infrastructure",
    "usecase",
}
MAPPER_FORBIDDEN = {
    "json",
    "fcntl",
    "subprocess",
    "tempfile",
    "repository",
    "terminal",
    "application",
    "command",
    "cli",
    "infrastructure",
    "usecase",
}
SERVICE_FORBIDDEN = ENTITY_FORBIDDEN | {"pydantic"}


def test_domain_file_classification_covers_every_file_on_disk() -> None:
    # entity/mapper/service globs above are non-recursive per subdirectory,
    # so a new domain subpackage (or a nested directory inside one of the
    # three) would otherwise go unclassified and unchecked. rglob the whole
    # domain tree and require it to match exactly what the three category
    # lists cover.
    discovered = {
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if p.name != "__init__.py"
    }
    classified = (
        {f"entity/{f}" for f in ENTITY_FILES}
        | {f"mapper/{f}" for f in MAPPER_FILES}
        | {f"service/{f}" for f in SERVICE_FILES}
    )
    assert discovered == classified


@pytest.mark.parametrize("filename", ENTITY_FILES)
def test_domain_entity_file_does_not_import_repository_or_io_primitives(
    filename: str,
) -> None:
    assert_no_forbidden_imports(SRC / "entity" / filename, ENTITY_FORBIDDEN)


@pytest.mark.parametrize("filename", MAPPER_FILES)
def test_domain_mapper_file_does_not_import_repository_or_io_primitives(
    filename: str,
) -> None:
    assert_no_forbidden_imports(SRC / "mapper" / filename, MAPPER_FORBIDDEN)


@pytest.mark.parametrize("filename", SERVICE_FILES)
def test_domain_service_file_does_not_import_repository_terminal_application_command_cli_or_pydantic(
    filename: str,
) -> None:
    assert_no_forbidden_imports(SRC / "service" / filename, SERVICE_FORBIDDEN)
