"""Tests for the persistent, size-bounded debug-log configuration.

Why these tests exist:
  The board runs as a long-lived, systemd-restarted service. The debug log used
  to open with mode="w", so (a) nothing survived a restart -- the log was wiped
  every process start, destroying the evidence of anything that happened before
  the last boot -- and (b) the only thing bounding growth was that truncation.
  The handler is now an appending, size-rotated handler, and the two app
  processes (main + Flask web) write to separate files so a shared rotating
  handler can't race on rollover. These tests guard all three properties.
"""

import importlib
import logging

import universalchess.board.logging as board_logging


def _restore_root_logger():
    """Detach handlers added to the shared root logger during a test."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: S110  # nosec B110 - test cleanup; a close failure must not mask the assertion result
            pass


def test_debug_log_appends_across_restarts(tmp_path):
    """A second setup_logging (simulating a restart) must not wipe the log.

    Regression manifestation: reverting to mode="w" makes the second call
    truncate the file, so the "before restart" line disappears and only the
    post-restart line remains -- exactly the evidence loss this change fixes.
    """
    log_path = tmp_path / "debug.log"
    try:
        board_logging.setup_logging(str(log_path))
        logging.getLogger().info("before restart")
        _restore_root_logger()

        board_logging.setup_logging(str(log_path))
        logging.getLogger().info("after restart")
        _restore_root_logger()

        contents = log_path.read_text()
        assert "before restart" in contents
        assert "after restart" in contents
    finally:
        _restore_root_logger()


def test_debug_log_is_size_bounded(tmp_path, monkeypatch):
    """Total on-disk size is capped at (backup_count + 1) * max_bytes.

    Regression manifestation: a plain FileHandler (no rotation) grows without
    limit while the service runs, so this asserts rotation actually happens and
    the number of retained files is bounded. Uses tiny limits so a handful of
    lines forces several rollovers.
    """
    max_bytes = 200
    backup_count = 2
    monkeypatch.setattr(board_logging, "DEBUG_LOG_MAX_BYTES", max_bytes)
    monkeypatch.setattr(board_logging, "DEBUG_LOG_BACKUP_COUNT", backup_count)

    log_path = tmp_path / "debug.log"
    try:
        board_logging.setup_logging(str(log_path))
        root = logging.getLogger()
        # Each record is well over max_bytes worth once formatted across many
        # writes, forcing multiple rollovers.
        for i in range(100):
            root.info("log line %d padding-padding-padding-padding", i)
        _restore_root_logger()

        rotated = sorted(tmp_path.glob("debug.log*"))
        # Current file + at most backup_count rotations, nothing more retained.
        assert len(rotated) <= backup_count + 1
        # Rotation actually occurred (not a single unbounded file).
        assert (tmp_path / "debug.log.1").exists()
        # Every retained file honors the per-file size cap (small slack for the
        # final record that triggers the next rollover check).
        for path in rotated:
            assert path.stat().st_size <= max_bytes * 2
    finally:
        _restore_root_logger()


def test_default_log_path_is_per_process(monkeypatch, tmp_path):
    """Main gets ~/debug.log; the Flask web process gets its own file.

    Regression manifestation: if both processes resolved to ~/debug.log they
    would share one RotatingFileHandler and race on rollover (one renames the
    file the other still holds open), losing lines and defeating the cap. The
    web process is distinguished by the FLASK_RUN_PORT its unit sets.
    """
    monkeypatch.setattr(board_logging.Path, "home", classmethod(lambda cls: tmp_path))

    monkeypatch.delenv("FLASK_RUN_PORT", raising=False)
    assert board_logging._default_log_path() == str(tmp_path / "debug.log")

    monkeypatch.setenv("FLASK_RUN_PORT", "5000")
    assert board_logging._default_log_path() == str(tmp_path / "debug-web.log")
