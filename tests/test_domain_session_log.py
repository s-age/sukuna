from sukuna.domain.service.session_log import (
    encode_project_dir_name,
    find_custom_title_match,
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
