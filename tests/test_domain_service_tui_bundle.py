from sukuna.domain.service.tui_bundle import stamp_matches_source


def test_matches_when_both_fingerprints_are_equal() -> None:
    assert stamp_matches_source("abc123", "abc123") is True


def test_does_not_match_when_fingerprints_differ() -> None:
    assert stamp_matches_source("abc123", "def456") is False


def test_does_not_match_when_stamped_fingerprint_is_none() -> None:
    assert stamp_matches_source(None, "abc123") is False


def test_does_not_match_when_current_fingerprint_is_none() -> None:
    assert stamp_matches_source("abc123", None) is False


def test_does_not_match_when_both_are_none() -> None:
    assert stamp_matches_source(None, None) is False
