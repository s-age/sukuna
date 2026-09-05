import json
from pathlib import Path

from sukuna.domain.mapper.registry_mapper import WorkerRecordDocument

INDEX_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "sukuna" / "schemas" / "index.json"
)


def test_schemas_index_matches_a_freshly_generated_worker_record_document_schema() -> (
    None
):
    """`schemas/index.json` is a generated artifact -- no hand-written
    schema, no frozen archive of past versions; git history is the
    archive. This pins it to `WorkerRecordDocument.model_json_schema()`
    so the two cannot drift apart silently."""
    regenerated = (
        json.dumps(WorkerRecordDocument.model_json_schema(), indent=2, sort_keys=True)
        + "\n"
    )

    assert INDEX_PATH.read_text(encoding="utf-8") == regenerated
