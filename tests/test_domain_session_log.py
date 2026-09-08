import pytest

from sukuna.domain.service.session_log import (
    PERMISSION_MODE_CHOICES,
    encode_project_dir_name,
    extract_model,
    extract_permission_mode,
    find_custom_title_match,
    resolve_respawn_model,
    validate_permission_mode,
)


def test_encode_project_dir_name_replaces_non_alnum_with_dashes() -> None:
    assert (
        encode_project_dir_name(
            "/Users/s-age/gitrepos/sukuna-worktrees/tui-right-pane-split"
        )
        == "-Users-s-age-gitrepos-sukuna-worktrees-tui-right-pane-split"
    )


def test_encode_project_dir_name_is_deterministic() -> None:
    cwd = "/Users/example/some project (dir)"
    assert encode_project_dir_name(cwd) == encode_project_dir_name(cwd)


def test_encode_project_dir_name_hashes_paths_over_the_length_threshold() -> None:
    cwd = "/Users/example/" + "x" * 250

    encoded = encode_project_dir_name(cwd)
    sanitized_prefix = encoded.rsplit("-", 1)[0]

    assert len(sanitized_prefix) == 200
    assert encoded != sanitized_prefix
    assert encode_project_dir_name(cwd) == encoded


def test_find_custom_title_match_true_when_an_entry_matches() -> None:
    entries = [
        {"type": "custom-title", "customTitle": "ccw-x-1", "sessionId": "abc"},
        {"type": "agent-name"},
    ]

    assert find_custom_title_match(entries, "ccw-x-1") is True


def test_find_custom_title_match_false_when_name_differs() -> None:
    entries = [{"type": "custom-title", "customTitle": "ccw-x-1", "sessionId": "abc"}]

    assert find_custom_title_match(entries, "ccw-x-2") is False


def test_find_custom_title_match_skips_non_dict_entries() -> None:
    entries = [
        "not a dict",
        {"type": "custom-title", "customTitle": "ccw-x-1", "sessionId": "abc"},
    ]

    assert find_custom_title_match(entries, "ccw-x-1") is True


def test_find_custom_title_match_false_for_no_entries() -> None:
    assert find_custom_title_match([], "ccw-x-1") is False


def test_extract_permission_mode_returns_the_value_for_a_permission_mode_entry() -> (
    None
):
    entry = {"type": "permission-mode", "permissionMode": "auto"}

    assert extract_permission_mode(entry) == "auto"


def test_extract_permission_mode_returns_none_for_a_different_type_or_non_str_value() -> (
    None
):
    assert extract_permission_mode({"type": "agent-name"}) is None
    assert extract_permission_mode({"type": "permission-mode"}) is None
    assert (
        extract_permission_mode({"type": "permission-mode", "permissionMode": 1})
        is None
    )


def test_extract_permission_mode_returns_none_for_a_non_dict_entry() -> None:
    assert extract_permission_mode("not a dict") is None
    assert extract_permission_mode(None) is None


@pytest.mark.parametrize("choice", PERMISSION_MODE_CHOICES)
def test_validate_permission_mode_accepts_each_of_the_six_choices(choice: str) -> None:
    assert validate_permission_mode(choice) == choice


def test_validate_permission_mode_rejects_the_default_sentinel() -> None:
    assert validate_permission_mode("default") is None


def test_validate_permission_mode_rejects_an_unknown_value() -> None:
    assert validate_permission_mode("some-future-mode") is None


def test_validate_permission_mode_passes_through_none() -> None:
    assert validate_permission_mode(None) is None


def test_extract_model_returns_the_value_for_a_message_model_entry() -> None:
    entry = {"message": {"model": "claude-opus-5"}}

    assert extract_model(entry) == "claude-opus-5"


def test_extract_model_returns_none_when_message_is_missing_or_not_a_dict() -> None:
    assert extract_model({}) is None
    assert extract_model({"message": "not a dict"}) is None


def test_extract_model_returns_none_for_a_non_string_model_value() -> None:
    assert extract_model({"message": {"model": 1}}) is None
    assert extract_model({"message": {}}) is None


def test_extract_model_returns_none_for_a_non_dict_entry() -> None:
    assert extract_model("not a dict") is None
    assert extract_model(None) is None


def test_extract_model_returns_none_for_the_synthetic_sentinel_value() -> None:
    assert extract_model({"message": {"model": "<synthetic>"}}) is None


def test_resolve_respawn_model_prefers_the_jsonl_derived_value() -> None:
    assert resolve_respawn_model("claude-opus-5", "claude-sonnet-5") == "claude-opus-5"


def test_resolve_respawn_model_falls_back_to_the_spawn_time_value_when_jsonl_is_none() -> (
    None
):
    assert resolve_respawn_model(None, "claude-sonnet-5") == "claude-sonnet-5"


def test_resolve_respawn_model_returns_none_when_both_sources_are_none() -> None:
    assert resolve_respawn_model(None, None) is None
