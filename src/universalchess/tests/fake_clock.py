"""A hand-advanced monotonic clock for deterministic timing tests.

Code that measures elapsed time takes its time source by injection
(``ChessGameState._now_monotonic``, ``ChessClockState._now_fn``, the
``monotonic=`` parameters on the system service) precisely so tests can supply a
clock they control. Otherwise a test asserting "this move took 9 seconds" has to
sleep for 9 seconds, and still fails intermittently on a loaded machine.

Centralised because several test modules had each grown their own copy of the
same four-line class. They are trivial to write, which is exactly why they
proliferate; sharing one keeps a change to the injection convention a single
edit rather than a hunt through the suite.
"""


class FakeMonotonic:
    """A monotonic time source that only advances when a test says so.

    Callable like ``time.monotonic``, so it drops directly into any injection
    point expecting one. Advance it with :meth:`advance`, or by assigning to
    ``now`` for tests that read more naturally that way.

    The default start is deliberately far from zero: a clock starting at 0.0
    makes an "unset anchor" bug (which also reads as 0.0) look like a correct
    measurement of no elapsed time.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds``."""
        self.now += seconds
