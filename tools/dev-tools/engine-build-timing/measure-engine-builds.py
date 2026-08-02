#!/usr/bin/env python3
"""Measure real engine build times on a board over ssh.

Why this exists: the catalog's ``estimated_install_minutes`` paces the install
progress bar and, together with the build timeout, decides whether an install is
killed mid-compile. Both were set by hand against fast hardware, which is how a
Pi Zero W came to fail Rodent IV with "Build timed out after 600s" while the
compiler was still working. This produces the measured figure for a given board
so those numbers can be set from data.

The build commands are read from ``ENGINES`` in the repo, never retyped here, so
what gets timed is exactly what the installer runs. The remote half
(``uc-build-timer``) is copied to the board and started detached, so a dropped
ssh session neither kills the build nor loses the result.

Usage:
    ./measure-engine-builds.py --host pa@dgt-zero-w.local rodentIV
    ./measure-engine-builds.py --host pa@dgt-zero-w.local --all-source-built
    ./measure-engine-builds.py --host pa@dgt-zero-w.local --watch-only rodentIV
    ./measure-engine-builds.py --host pa@dgt-zero-w.local --collect
"""

import argparse
import json
import shlex
import subprocess  # nosec B404  # subprocess use is intentional; each call runs a fixed ssh/scp argv list with a developer-supplied host, never shell=True
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from universalchess.managers.engine_manager import (  # noqa: E402
    ENGINES,
    _BUILD_JOB_MB,
    engine_supports_arch,
)

_REMOTE_TIMER = "/tmp/uc-build-timer"  # noqa: S108  # nosec B108  # path on the remote board, not a local temp file
_REMOTE_WORKDIR = "/var/tmp/uc-build-timing"  # noqa: S108  # nosec B108  # path on the remote board, not a local temp file
_LOCAL_RESULTS = Path(__file__).resolve().parent / "results"

# Keep ssh from wedging the watcher when a board drops off Wi-Fi mid-build: a
# hung control connection would look identical to a stalled build.
_SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
]


def ssh(host: str, command: str, timeout: Optional[float] = 60.0) -> subprocess.CompletedProcess:
    """Run a command on the board and return the completed process."""
    return subprocess.run(  # noqa: S603  # nosec B603 B607  # developer-supplied ssh target from a local dev tool, never a request path; ssh comes from PATH like the repo's other board scripts
        ["ssh", *_SSH_OPTS, host, command],  # noqa: S607
        capture_output=True, text=True, timeout=timeout,
    )


def remote_arch(host: str) -> str:
    """The board's architecture token, matching ``get_current_arch``.

    Needed so the driver refuses to time an engine the installer would never
    build there (Berserk on 32-bit ARM), which would otherwise record a
    meaningless compile failure as if it were a timing.
    """
    machine = ssh(host, "uname -m").stdout.strip().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("armv7l", "armv6l", "arm"):
        return "armhf"
    return "arm64" if "64" in machine else "armhf"


def describe_board(host: str) -> str:
    """One-line identity of the board, for the run header."""
    out = ssh(host, "tr -d '\\0' < /proc/device-tree/model; echo; nproc; "
                    "awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo; uname -m").stdout
    parts = [line.strip() for line in out.splitlines() if line.strip()]
    if len(parts) < 4:
        return "unknown board"
    return f"{parts[0]} -- {parts[1]} core(s), {parts[2]} MB RAM, {parts[3]}"


def stage_timer(host: str) -> None:
    """Copy the remote timer onto the board and make it executable."""
    local = Path(__file__).resolve().parent / "uc-build-timer"
    proc = subprocess.run(  # noqa: S603  # nosec B603 B607  # developer-supplied ssh target from a local dev tool, never a request path; scp comes from PATH like the repo's other board scripts
        ["scp", *_SSH_OPTS, str(local), f"{host}:{_REMOTE_TIMER}"],  # noqa: S607
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"could not copy the timer to {host}: {proc.stderr.strip()}")
    ssh(host, f"chmod +x {_REMOTE_TIMER}")


def verify_toolchain(host: str) -> None:
    """Fail early if the board cannot compile at all.

    A missing gcc or git would otherwise show up as a build failure several
    minutes in and be mistaken for an engine problem.
    """
    missing = [
        tool for tool in ("git", "gcc", "g++", "make")
        if ssh(host, f"command -v {tool} >/dev/null 2>&1; echo $?").stdout.strip() != "0"
    ]
    if missing:
        raise SystemExit(
            f"{host} is missing build tools: {', '.join(missing)}. "
            "Install an engine through the app once, or apt-get install build-essential git."
        )


def _assert_not_already_running(host: str, engine_name: str) -> None:
    """Refuse to start a second timer for an engine already being measured.

    Two runs share one workdir and one checkout, so a second launch does not just
    skew the timing -- concurrent makes overwrite each other's object files, and
    both timers write the same progress.json, so the watcher reads alternating
    documents from two different runs. That happened: it corrupted one build until
    it exited, and produced a progress trace whose elapsed time moved backwards.
    """
    pattern = f"uc-build-timer --engine {engine_name} "
    # The bracket trick keeps the pgrep-style match from finding this ssh command.
    probe = f"ps -eo pid,args | grep '[{pattern[0]}]{pattern[1:]}' || true"
    running = ssh(host, probe).stdout.strip()
    if running:
        raise SystemExit(
            f"a timer for {engine_name} is already running on {host}:\n{running}\n"
            f"watch it with --watch-only, or stop it by pid before starting a new run"
        )


def start_build(host: str, engine_name: str, with_build_memory: bool = True) -> None:
    """Launch the timer on the board, detached from this ssh session."""
    _assert_not_already_running(host, engine_name)
    engine = ENGINES[engine_name]
    build_command = "\n".join(engine.build_commands)
    argv = [
        _REMOTE_TIMER,
        "--engine", engine_name,
        "--build-command", build_command,
        "--workdir", _REMOTE_WORKDIR,
        "--job-memory-mb", str(_BUILD_JOB_MB),
    ]
    if with_build_memory:
        argv.append("--with-build-memory")
    if engine.repo_url:
        argv += ["--repo-url", engine.repo_url]
    if engine.git_ref:
        argv += ["--git-ref", engine.git_ref]

    quoted = " ".join(shlex.quote(part) for part in argv)
    log = f"{_REMOTE_WORKDIR}/{engine_name}/timer.log"
    # setsid + nohup + redirected streams: without all three the timer dies with
    # the ssh session, and a multi-hour build would have to be restarted on every
    # network blip.
    #
    # The mkdir is a separate statement on purpose. Joined with "&&", the trailing
    # "&" backgrounds the whole "mkdir && setsid" list, so bash forks a subshell
    # for it -- and that subshell inherits ssh's stdout pipe even though the timer
    # itself has its streams redirected. ssh then blocks waiting for EOF on that
    # pipe until the build ends, which defeats the point of detaching and makes
    # the launch appear to hang for the entire build.
    # Both documents are removed before launching. The watcher treats the presence
    # of result.json as "this run finished", so a leftover file from an earlier run
    # is reported as if it were this run's timing -- silently, and with plausible
    # numbers, which is the worst kind of wrong for a measurement tool.
    out_dir = f"{_REMOTE_WORKDIR}/{engine_name}"
    launch = (
        f"mkdir -p {out_dir} || exit 1; "
        f"rm -f {out_dir}/result.json {out_dir}/progress.json; "
        f"setsid nohup {quoted} > {shlex.quote(log)} 2>&1 < /dev/null & echo started"
    )
    proc = ssh(host, launch)
    if "started" not in proc.stdout:
        raise SystemExit(f"could not start the build on {host}: {proc.stderr.strip()}")


def read_progress(host: str, engine_name: str) -> Optional[Dict[str, object]]:
    """Latest progress document for ``engine_name``, or None if not readable yet."""
    path = f"{_REMOTE_WORKDIR}/{engine_name}/progress.json"
    proc = ssh(host, f"cat {shlex.quote(path)} 2>/dev/null")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def read_result(host: str, engine_name: str) -> Optional[Dict[str, object]]:
    """Final result document for ``engine_name``, or None while still building."""
    path = f"{_REMOTE_WORKDIR}/{engine_name}/result.json"
    proc = ssh(host, f"cat {shlex.quote(path)} 2>/dev/null")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _progress_line(progress: Dict[str, object]) -> str:
    """One-line live status, including the ETA when the timer can justify one."""
    elapsed = _format_duration(progress.get("elapsed_seconds"))
    done = progress.get("units_finished")
    total = progress.get("units_total")
    exactness = "" if progress.get("total_is_exact") else "~"
    percent = progress.get("fraction")
    percent_text = f"{float(percent) * 100:4.0f}%" if isinstance(percent, (int, float)) else "   ?"
    eta = _format_duration(progress.get("eta_seconds"))
    current = progress.get("current_unit") or "-"
    avail = progress.get("min_mem_available_mb")
    gap_cpu = progress.get("max_gap_seconds_cpu_only")
    gap_io = progress.get("max_gap_seconds_with_io")
    return (
        f"{progress.get('phase'):<9} elapsed {elapsed:>7}  "
        f"units {done}/{exactness}{total} {percent_text}  eta {eta:>7}  "
        f"now {str(current)[:24]:<24} min_free {avail}MB  "
        f"max_quiet cpu={gap_cpu}s io={gap_io}s"
    )


def watch(host: str, engine_name: str, poll_seconds: float) -> Optional[Dict[str, object]]:
    """Print live progress until the build finishes; return its result document."""
    print(f"watching {engine_name} on {host} (Ctrl-C stops watching, not the build)")
    silent_polls = 0
    while True:
        result = read_result(host, engine_name)
        if result is not None:
            print(f"\n{engine_name}: {result.get('phase')} in "
                  f"{_format_duration(result.get('build_seconds'))} "
                  f"(clone {_format_duration(result.get('clone_seconds'))}, "
                  f"{result.get('units_compiled')}/{result.get('units_total')} units, "
                  f"peak RSS {result.get('peak_build_rss_mb')}MB, "
                  f"longest quiet stretch {result.get('max_gap_seconds_cpu_only')}s cpu-only / "
                  f"{result.get('max_gap_seconds_with_io')}s with-io)")
            return result
        progress = read_progress(host, engine_name)
        if progress is None:
            silent_polls += 1
            if silent_polls > 5:
                print(f"no progress file yet after {silent_polls} polls; "
                      f"check {_REMOTE_WORKDIR}/{engine_name}/timer.log on the board")
                silent_polls = 0
        else:
            silent_polls = 0
            print(f"  {_progress_line(progress)}", flush=True)
        time.sleep(poll_seconds)


def save_result(host: str, result: Dict[str, object]) -> Path:
    """Store a result document locally, keyed by board and engine."""
    _LOCAL_RESULTS.mkdir(parents=True, exist_ok=True)
    safe_host = host.replace("@", "_at_").replace(":", "_")
    path = _LOCAL_RESULTS / f"{safe_host}-{result['engine']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


def source_built_engines(arch: str) -> List[str]:
    """Catalog engines that compile on ``arch``, cheapest estimate first.

    Ordered by the existing estimate so a run on a slow board produces several
    short data points before committing hours to the largest build.
    """
    names = [
        name for name, engine in ENGINES.items()
        if engine.build_commands and engine.repo_url and engine_supports_arch(engine, arch)
    ]
    return sorted(names, key=lambda name: ENGINES[name].estimated_install_minutes)


def report(results: Sequence[Dict[str, object]]) -> None:
    """Print measured versus catalogued figures, and the headroom each implies."""
    if not results:
        return
    print("\nmeasured vs catalog")
    print(f"{'engine':<14}{'measured':>10}{'estimate':>10}{'timeout':>9}"
          f"{'ratio':>7}{'timeout/measured':>18}")
    for result in results:
        name = str(result["engine"])
        engine = ENGINES.get(name)
        if engine is None:
            continue
        measured = result.get("build_seconds")
        if not isinstance(measured, (int, float)) or measured <= 0:
            continue
        estimate = engine.estimated_install_minutes * 60
        ratio = measured / estimate if estimate else float("inf")
        margin = engine.build_timeout / measured
        flag = "  <-- would time out" if margin < 1.0 else ""
        print(f"{name:<14}{_format_duration(measured):>10}{_format_duration(estimate):>10}"
              f"{_format_duration(engine.build_timeout):>9}{ratio:>7.2f}{margin:>18.2f}{flag}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engines", nargs="*", help="Catalog engine names to measure")
    parser.add_argument("--host", required=True, help="ssh target, e.g. pa@dgt-zero-w.local")
    parser.add_argument("--all-source-built", action="store_true",
                        help="Measure every engine that compiles on this board")
    parser.add_argument("--watch-only", action="store_true",
                        help="Attach to a build already running on the board")
    parser.add_argument("--collect", action="store_true",
                        help="Fetch and report every result already on the board")
    parser.add_argument("--poll-seconds", type=float, default=20.0,
                        help="Seconds between progress polls")
    parser.add_argument("--no-build-memory", action="store_true",
                        help="Build without the installer's temporary swap, to isolate its effect")
    args = parser.parse_args(argv)

    arch = remote_arch(args.host)
    print(f"board: {describe_board(args.host)}  (arch token: {arch})")

    if args.collect:
        collected = []
        for name in source_built_engines(arch):
            result = read_result(args.host, name)
            if result is not None:
                save_result(args.host, result)
                collected.append(result)
        report(collected)
        return 0

    names = source_built_engines(arch) if args.all_source_built else list(args.engines)
    if not names:
        parser.error("name at least one engine, or pass --all-source-built")

    unknown = [name for name in names if name not in ENGINES]
    if unknown:
        parser.error(f"not in the catalog: {', '.join(unknown)}")
    unsupported = [name for name in names if not engine_supports_arch(ENGINES[name], arch)]
    if unsupported:
        parser.error(f"not buildable on {arch}: {', '.join(unsupported)}")

    if not args.watch_only:
        verify_toolchain(args.host)
        stage_timer(args.host)

    results = []
    for name in names:
        if not args.watch_only:
            print(f"\nstarting {name} "
                  f"(catalog estimate {ENGINES[name].estimated_install_minutes} min, "
                  f"timeout {ENGINES[name].build_timeout}s, "
                  f"build swap {'off' if args.no_build_memory else 'on'})")
            start_build(args.host, name, with_build_memory=not args.no_build_memory)
        result = watch(args.host, name, args.poll_seconds)
        if result is not None:
            saved = save_result(args.host, result)
            print(f"  saved {saved}")
            results.append(result)

    report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
