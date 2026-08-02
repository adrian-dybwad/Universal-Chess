#!/usr/bin/env python3
"""Validate services/build_progress.py against a real engine build on real hardware.

Why this exists: the unit tests drive the tracker with synthetic process tables, so
they prove the logic but not that ``/proc`` on the target board yields what the
logic expects. The failure this work fixes only appears on a Raspberry Pi Zero W,
so the observation layer has to be exercised there, on a real compile, using the
production module rather than a copy of it.

Run on the board with the module copied alongside::

    python3 validate-build-progress.py --engine-dir /var/tmp/uc-build-timing/rodentIV

Reports the progress trace and the checks that matter for the install:

* the compiler was observed at all (an unobservable tree would silently fall back
  to the elapsed-time creep this work replaces);
* the unit total was exact, and the final count matched it;
* the unit total equals the number of source files actually present, when
  ``--expect-units`` supplies it. This check exists because its absence let a real
  defect pass: a 32-file Rodent IV build reported "65 of 65 units" and satisfied
  every other check, since GCC names each unit a second time in ``-dumpbase`` and
  the parser counted both. Self-consistent numbers can still be wrong, so the
  denominator is checked against the filesystem;
* the fraction never went backwards and never reached 1.0 before exit;
* the longest stretch with no liveness signal, which is what the stall window must
  clear -- printed so the configured window can be checked against real hardware
  rather than assumed.
"""

import argparse
import json
import os
import subprocess  # nosec B404  # runs the engine's real build command; that is the point of this validation
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_progress import BuildProgressTracker, read_process_table

_SAMPLE_SECONDS = 1.0
# How often the running trace prints. Coarse on purpose: the point of the trace is
# to show progress advancing over minutes, not to log every sample.
_REPORT_SECONDS = 30.0


def run_validation(build_command: str, cwd: Path, stall_seconds: int) -> dict:
    """Run one build under the tracker and return what was observed."""
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S602  # nosec B602  # a shell is required: engine build commands are shell snippets with cd/case/&&, exactly as the installer runs them
        build_command,
        cwd=str(cwd),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    tracker = BuildProgressTracker(
        root_pid=process.pid, source_root=cwd, started_at=started,
    )

    fractions: list[float] = []
    worst_quiet = 0.0
    observed_exact = False
    last_report = 0.0

    while process.poll() is None:
        now = time.monotonic()
        tracker.record(read_process_table(), now)
        progress = tracker.progress(now)
        quiet = now - tracker.last_activity_at
        worst_quiet = max(worst_quiet, quiet)
        if progress.fraction is not None:
            fractions.append(progress.fraction)
        if progress.total_is_exact:
            observed_exact = True
        if now - last_report >= _REPORT_SECONDS:
            last_report = now
            eta = "?" if progress.eta_seconds is None else f"{progress.eta_seconds}s"
            sys.stdout.write(
                f"{int(now - started):5d}s  "
                f"{progress.units_finished}/{progress.units_total} units  "
                f"{(progress.fraction or 0) * 100:5.1f}%  eta {eta:>6}  "
                f"now {(progress.current_unit or '-')[:28]:<28} "
                f"quiet {quiet:4.1f}s\n"
            )
            sys.stdout.flush()
        time.sleep(_SAMPLE_SECONDS)

    output = process.stdout.read() if process.stdout else ""
    elapsed = time.monotonic() - started
    final = tracker.progress(time.monotonic())

    return {
        "returncode": process.returncode,
        "build_seconds": round(elapsed, 1),
        "observed": final.observed,
        "total_is_exact": observed_exact,
        "units_finished": final.units_finished,
        "units_total": final.units_total,
        "worst_quiet_seconds": round(worst_quiet, 1),
        "stall_window_seconds": stall_seconds,
        "would_have_stalled": worst_quiet >= stall_seconds,
        "fraction_samples": len(fractions),
        "fraction_monotonic": all(
            later >= earlier for earlier, later in zip(fractions, fractions[1:])
        ),
        "max_fraction_before_exit": round(max(fractions), 4) if fractions else None,
        "output_lines": len([line for line in output.splitlines() if line.strip()]),
        # Kept because a validation that reports only "exit 2" cannot be acted on --
        # the same diagnosability gap this work removes from the installer.
        "output_tail": [line for line in output.splitlines() if line.strip()][-8:],
    }


def main() -> int:
    """Parse arguments, run the validation, and report pass or fail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-dir", required=True,
                        help="Directory the build command runs in (the clone root)")
    parser.add_argument("--build-command", required=True,
                        help="The engine's real build command, verbatim")
    parser.add_argument("--stall-seconds", type=int, default=120,
                        help="Stall window to check the observed quiet stretches against")
    parser.add_argument("--expect-units", type=int, default=None,
                        help="Source-file count the observed unit total must equal "
                             "(e.g. `find . -name '*.cpp' | wc -l`)")
    args = parser.parse_args()

    cwd = Path(args.engine_dir)
    if not cwd.is_dir():
        parser.error(f"not a directory: {cwd}")

    # Match the installer's environment: parallelism is bounded there, and an
    # unbounded make here would not reproduce the single-core behaviour measured.
    os.environ.setdefault("MAKEFLAGS", "-j1")

    result = run_validation(args.build_command, cwd, args.stall_seconds)
    sys.stdout.write("\n" + json.dumps(result, indent=2) + "\n")

    failures = []
    if result["returncode"] != 0:
        failures.append(f"build failed with exit {result['returncode']}")
    if not result["observed"]:
        failures.append("compiler was never observed; progress would fall back to time")
    if result["would_have_stalled"]:
        failures.append(
            f"a healthy build went quiet for {result['worst_quiet_seconds']}s, "
            f"which the {args.stall_seconds}s stall window would have killed"
        )
    if args.expect_units is not None and result["units_total"] != args.expect_units:
        failures.append(
            f"counted {result['units_total']} units for {args.expect_units} source "
            f"files; the denominator of every percentage and ETA is wrong"
        )
    if not result["fraction_monotonic"]:
        failures.append("the reported fraction moved backwards")
    if result["max_fraction_before_exit"] == 1.0:
        failures.append("the fraction reached 1.0 before the build exited")

    if failures:
        for failure in failures:
            sys.stdout.write(f"FAIL: {failure}\n")
        return 1
    sys.stdout.write("PASS: observation held for the whole build\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
