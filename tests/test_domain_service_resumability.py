from pathlib import Path

from _terminal_fakes import make_worker

from sukuna.domain.service.resumability import is_resumable


def test_is_resumable_true_when_worktree_matches_cwd(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    assert is_resumable(worker, str(tmp_path)) is True


def test_is_resumable_false_when_worktree_differs_from_cwd(
    tmp_path: Path,
) -> None:
    worker = make_worker(tmp_path, parent_session_id="orchestrator-1", suffix="a")

    assert is_resumable(worker, str(tmp_path / "elsewhere")) is False


def test_is_resumable_matches_through_a_symlinked_cwd(tmp_path: Path) -> None:
    """`WorkerRecord.worktree` is stored `expanduser().resolve()`d at spawn
    time (`domain/service/spawn_preflight.py`) -- a `cwd` that reaches the
    same real directory through a symlink must resolve to the identical
    string for `is_resumable()`'s plain `==` to hold, exercising the
    normalization contract end to end rather than just the comparison
    itself."""
    real_dir = tmp_path / "real-worktree"
    real_dir.mkdir()
    symlink = tmp_path / "symlinked-worktree"
    symlink.symlink_to(real_dir)
    worker = make_worker(
        tmp_path, parent_session_id="orchestrator-1", suffix="a", worktree=real_dir
    )
    cwd_via_symlink = str(symlink.resolve())

    assert worker.worktree == str(real_dir.resolve())
    assert is_resumable(worker, cwd_via_symlink) is True
