"""Tests for caching the UCI option probe.

Root cause these guard
----------------------
``GET /api/engines/<name>/uci-schema`` (aliased ``/profiles``) is unauthenticated,
and every call reached ``probe_options``, which launches the engine binary,
reads its advertised options, and reaps it. On a Pi Zero a loop of requests
therefore spawned an unbounded stream of engine processes -- an anonymous
CPU-exhaustion vector that also starves the game engine mid-move.

An engine's advertised options depend only on the binary, so the probe result is
cacheable. The cache is keyed on the binary's identity (path + size + mtime) rather
than a timer, so a reinstalled or rebuilt engine re-probes immediately instead of
serving stale options for the rest of a TTL.

Only the probe is cached, never the built schema groups: ``build_groups`` enumerates
selectable net files from the engines directory, so caching the groups would hide
nets added by a repair or top-up.

Concurrent misses are serialised, because a cache that only helps *after* the first
probe completes still lets N simultaneous requests spawn N processes.
"""

import threading

import pytest

from universalchess.services import uci_schema


class FakeOption:
    """Stand-in for a python-chess UCI Option (only the name is inspected here)."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"FakeOption({self.name!r})"


@pytest.fixture
def engine_binary(tmp_path):
    """A file standing in for an engine binary, so stat() has something to key on."""
    binary = tmp_path / "fake-engine"
    binary.write_bytes(b"#!/bin/sh\n")
    return binary


@pytest.fixture(autouse=True)
def clear_cache():
    """Isolate tests: a cache shared between them would mask real misses."""
    uci_schema.clear_probe_cache()
    yield
    uci_schema.clear_probe_cache()


@pytest.fixture
def counting_probe(monkeypatch):
    """Replace the real engine launch with a counter.

    Mocks at the boundary between this module and the engine registry (the thing
    that spawns a process), so the caching logic itself is exercised for real.
    """
    calls = []

    def fake_launch(engine_path):
        calls.append(engine_path)
        return [FakeOption("Threads"), FakeOption("Hash")]

    monkeypatch.setattr(uci_schema, "_launch_and_read_options", fake_launch)
    return calls


class TestProbeCaching:
    """Repeat probes of an unchanged binary must not relaunch it."""

    def test_first_probe_launches_the_engine(self, engine_binary, counting_probe):
        """A cold cache must actually probe.

        Guards against a cache that returns empty options without ever launching,
        which would silently render an engine as having no configurable options.
        """
        options = uci_schema.probe_options(str(engine_binary))

        assert len(counting_probe) == 1
        assert [o.name for o in options] == ["Threads", "Hash"]

    def test_repeat_probe_is_served_from_cache(self, engine_binary, counting_probe):
        """A second probe of the same binary must not launch it again.

        This is the fix. How the regression manifests: removing the cache makes the
        launch count equal the request count, restoring the anonymous
        process-spawn vector.
        """
        for _ in range(25):
            uci_schema.probe_options(str(engine_binary))

        assert len(counting_probe) == 1

    def test_cached_result_has_the_same_contents(self, engine_binary, counting_probe):
        """A cache hit must return the same options as the miss did.

        A count-only assertion would pass even if the cache returned an empty list
        on hits, so compare the payload too.
        """
        first = uci_schema.probe_options(str(engine_binary))
        second = uci_schema.probe_options(str(engine_binary))

        assert [o.name for o in first] == [o.name for o in second]

    def test_distinct_engines_are_cached_separately(self, tmp_path, counting_probe):
        """Two different binaries must each be probed.

        How the regression manifests: a single-slot or path-insensitive cache would
        serve engine A's options for engine B, showing the wrong options in the
        profile editor -- worse than no cache.
        """
        first = tmp_path / "engine-a"
        second = tmp_path / "engine-b"
        first.write_bytes(b"a")
        second.write_bytes(b"b")

        uci_schema.probe_options(str(first))
        uci_schema.probe_options(str(second))
        uci_schema.probe_options(str(first))

        assert len(counting_probe) == 2


class TestCacheInvalidation:
    """A changed binary must be re-probed, not served from cache."""

    def test_reinstalled_binary_is_reprobed(self, engine_binary, counting_probe):
        """Replacing the binary must invalidate its cache entry.

        An engine upgrade or repair can change the advertised options. How the
        regression manifests: a path-only cache key keeps serving the old options,
        so newly added options never appear in the editor and removed ones linger.
        """
        import os

        uci_schema.probe_options(str(engine_binary))
        engine_binary.write_bytes(b"#!/bin/sh\n# rebuilt with more options\n")
        # Force a distinct mtime: a same-second rewrite can otherwise land on the
        # same timestamp, which would make this test pass for the wrong reason.
        stat = engine_binary.stat()
        os.utime(engine_binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        uci_schema.probe_options(str(engine_binary))

        assert len(counting_probe) == 2

    def test_missing_binary_is_not_cached_as_success(self, tmp_path, counting_probe):
        """A path that does not exist must still reach the launcher.

        The launcher owns the "binary missing" error classification. How the
        regression manifests: caching a stat failure as a successful empty probe
        would report a missing engine as installed-with-no-options.
        """
        uci_schema.probe_options(str(tmp_path / "absent-engine"))
        uci_schema.probe_options(str(tmp_path / "absent-engine"))

        assert len(counting_probe) == 2

    def test_clear_probe_cache_forces_a_reprobe(self, engine_binary, counting_probe):
        """The explicit clear must drop cached entries.

        Needed after an engine install/repair replaces binaries, and by tests. How
        the regression manifests: a no-op clear leaves the editor showing
        pre-install options after a successful install.
        """
        uci_schema.probe_options(str(engine_binary))
        uci_schema.clear_probe_cache()
        uci_schema.probe_options(str(engine_binary))

        assert len(counting_probe) == 2


class TestProbeFailuresAreNotCached:
    """A failed probe must not be remembered as a result."""

    def test_error_is_reraised_each_time(self, engine_binary, monkeypatch):
        """A launch failure must propagate on every call, not be cached.

        Caching a failure would make a transient startup problem permanent until
        restart; caching it as a *success* would be worse. How the regression
        manifests: the second call returns options (or None) instead of raising.
        """
        calls = []

        def failing_launch(engine_path):
            calls.append(engine_path)
            raise uci_schema.EngineProbeError("cannot start", reason_code="load_failed")

        monkeypatch.setattr(uci_schema, "_launch_and_read_options", failing_launch)

        for _ in range(2):
            with pytest.raises(uci_schema.EngineProbeError):
                uci_schema.probe_options(str(engine_binary))

        assert len(calls) == 2


class TestConcurrentProbesAreSerialised:
    """Simultaneous cold requests must not each spawn a process."""

    def test_parallel_misses_launch_once(self, engine_binary, monkeypatch):
        """Twelve concurrent probes of a cold cache must launch the engine once.

        This is the part that actually bounds the DoS: a cache checked without
        single-flight still lets a burst of simultaneous requests spawn one engine
        each. The launcher blocks on a barrier-like sleep so all threads are inside
        the critical window at once; without serialisation the launch count equals
        the thread count.
        """
        import time

        calls = []

        def slow_launch(engine_path):
            calls.append(engine_path)
            time.sleep(0.05)
            return [FakeOption("Threads")]

        monkeypatch.setattr(uci_schema, "_launch_and_read_options", slow_launch)

        threads = [
            threading.Thread(target=uci_schema.probe_options, args=(str(engine_binary),))
            for _ in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(calls) == 1
