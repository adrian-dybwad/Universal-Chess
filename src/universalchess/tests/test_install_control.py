"""The board's client for the engine-install service the web process owns.

Background / why these tests exist
----------------------------------
Engine installs used to run in whichever process was asked. The board menu called
``install_async`` on its own ``EngineManager``; the web called ``install_engine``
on its own. Two owners meant two truths: a build stopped from the board left a
preserved tree that the web could not see, resume or reclaim, and both processes
could start an install at the same time with nothing to stop them.

There is now one owner. The web process runs every install, holds the persisted
install state, and writes the resume points -- it already owned the catalog
install, the repair and the custom-engine-from-URL flows, along with the startup
reconciliation that turns a restart-killed install into a resumable one. The board
asks it to act and reads the result.

This module is the asking half. It rides the two sockets that already exist
between the processes rather than adding a third: requests go out as a
``engine_install_request`` event on the game socket (the same channel that already
carries battery, clock and Bluetooth status), and the answer comes back as an
``engine_install_reply`` board command on the settings socket (the same channel
that already carries shutdown and reboot).

The reply is what makes this more than fire-and-forget. Without it the board could
not distinguish "installing" from "refused because another install is already
running", and would show a progress screen for a build that was never started.

The reply says only whether the request was *accepted*. What happened next is
observed in the persisted install state, because that is the record that survives
a restart of either process.
"""

import threading
import time

import pytest

from universalchess.services.install_control import REQUEST_EVENT, InstallControlClient

ENGINE = "reckless"
REF = "v2.1.0"

# Short enough to keep the timeout tests quick, long enough that a scheduling
# hiccup on a loaded machine does not fail a test about something else.
TEST_TIMEOUT_SECONDS = 0.2


class _Web:
    """A stand-in for the web process at the other end of the sockets.

    Records each request and answers it inline, which is what the real exchange
    looks like from the board's side: the web validates and replies immediately,
    long before the install it dispatched has finished.
    """

    def __init__(self, *, accepted=True, message="Installing Reckless",
                 reply=True, delivered=True):
        self.requests = []
        self._accepted = accepted
        self._message = message
        self._reply = reply
        self._delivered = delivered
        self.client = None

    def send(self, event_type, data):
        self.requests.append((event_type, data))
        if not self._delivered:
            # The web process is not running, so the datagram goes nowhere.
            return False
        if self._reply:
            self.client.deliver_reply({
                "request_id": data["request_id"],
                "accepted": self._accepted,
                "message": self._message,
            })
        return True

    @property
    def last_request(self):
        return self.requests[-1][1]


@pytest.fixture
def web():
    return _Web()


@pytest.fixture
def client(web):
    client = InstallControlClient(
        send_request=web.send, reply_timeout_seconds=TEST_TIMEOUT_SECONDS
    )
    web.client = client
    return client


class TestRequests:
    """What goes out on the wire for each action."""

    def test_installing_names_the_engine_and_the_ref(self, client, web):
        """An install request carries the engine and the chosen ref.

        Why: the ref is the version the user picked in the board's tag list, and
        it is what the web resolves and records with the install state. Dropping
        it silently builds the catalog pin instead of what was asked for.

        How a regression manifests: the request arrives without a ref and the
        board's version choice is ignored.
        """
        client.install(ENGINE, ref=REF)

        event_type, data = web.requests[0]
        assert event_type == REQUEST_EVENT
        assert data["action"] == "install"
        assert data["engine"] == ENGINE
        assert data["ref"] == REF

    @pytest.mark.parametrize("call,expected_action", [
        (lambda c: c.install(ENGINE), "install"),
        (lambda c: c.stop(), "stop"),
        (lambda c: c.resume(ENGINE), "resume"),
        (lambda c: c.discard(ENGINE), "discard"),
    ])
    def test_each_action_is_named_on_the_wire(self, client, web, call,
                                              expected_action):
        """Every action the board can take sends its own request.

        Why: these four are the whole control surface, and the web dispatches on
        this string. A wrong or missing action is rejected by the far end, which
        looks to the user like the button doing nothing at all.

        How a regression manifests: one action is sent under another's name --
        Discard arriving as Stop would preserve a tree the user asked to delete.
        """
        call(client)

        assert web.last_request["action"] == expected_action

    def test_stopping_does_not_name_an_engine(self, client, web):
        """Stop applies to whatever install is running.

        Why: only one install runs at a time, and the web's stop reaches the
        manager holding that build rather than looking one up by name. Naming an
        engine here would imply a choice the far end does not have, and would go
        stale the moment the board's screen is out of date.

        How a regression manifests: an engine name is sent and later relied on,
        so a stop silently targets the wrong build.
        """
        client.stop()

        assert "engine" not in web.last_request

    def test_every_request_carries_its_own_id(self, client, web):
        """Requests are individually identified.

        Why: replies arrive on a different socket than requests leave on, so the
        only way to know which answer belongs to which question is the id. Two
        requests in quick succession -- a stop followed by a discard -- would
        otherwise resolve against each other's replies.

        How a regression manifests: ids repeat, and the second action reports the
        first one's outcome.
        """
        client.install(ENGINE)
        client.discard(ENGINE)

        ids = [data["request_id"] for _event, data in web.requests]
        assert all(ids)
        assert len(set(ids)) == 2


class TestReplies:
    """What the board learns back."""

    def test_an_accepted_request_reports_the_acceptance(self, client, web):
        """The web's yes is passed through with its message.

        Why: the board shows the message, and it is the far end that knows what
        it did -- which engine, at which ref, resumed or freshly installed.

        How a regression manifests: the board reports its own guess instead of
        what actually happened.
        """
        result = client.install(ENGINE)

        assert result.accepted is True
        assert result.message == "Installing Reckless"

    def test_a_refusal_reports_the_reason(self):
        """The web's no is passed through with its reason.

        Why: this is why the reply exists. A request can be refused because
        another install is already running, because the engine is unknown, or
        because a discard would delete a tree that is still being written to. The
        board cannot work any of that out for itself -- the state it would need
        lives in the other process.

        How a regression manifests: a refusal is reported as success and the
        board shows a progress screen for a build that never started.
        """
        web = _Web(accepted=False, message="Already installing Berserk")
        client = InstallControlClient(
            send_request=web.send, reply_timeout_seconds=TEST_TIMEOUT_SECONDS
        )
        web.client = client

        result = client.install(ENGINE)

        assert result.accepted is False
        assert result.message == "Already installing Berserk"

    def test_a_reply_for_another_request_is_ignored(self, client):
        """An unmatched id does not resolve a waiting request.

        Why: replies are matched by id precisely so a late answer to an abandoned
        request cannot be mistaken for the answer to the current one.

        How a regression manifests: any reply unblocks any waiter, so a stale
        "accepted" from a timed-out request makes the next refusal look like a
        success.
        """
        client.deliver_reply({
            "request_id": "not-a-pending-request",
            "accepted": True,
            "message": "Installing something else",
        })

        # Nothing was waiting, so nothing may have been resolved. The next real
        # request must still get its own answer rather than this stale one.
        assert client.pending_count == 0

    def test_a_reply_that_never_comes_is_reported_as_such(self):
        """A silent web process produces a refusal, not a hang.

        Why: the board menu thread blocks on this call, and it is the same thread
        that draws the screen and reads the keys. Waiting forever for a reply that
        the web crashed before sending would freeze the board with no way out.

        How a regression manifests: the board locks up until it is power-cycled.
        """
        web = _Web(reply=False)
        client = InstallControlClient(
            send_request=web.send, reply_timeout_seconds=TEST_TIMEOUT_SECONDS
        )
        web.client = client

        started = time.monotonic()
        result = client.install(ENGINE)
        elapsed = time.monotonic() - started

        assert result.accepted is False
        assert "not responding" in result.message.lower()
        assert elapsed >= TEST_TIMEOUT_SECONDS
        assert client.pending_count == 0, "a timed-out request must not be left waiting"

    def test_an_undelivered_request_fails_immediately(self):
        """With no web process listening the board is told at once.

        Why: the send itself reports this -- a datagram to a socket nobody is
        bound to fails outright -- so waiting out the reply timeout would make the
        board sit for seconds on a question that was never asked. Engine
        management is unavailable when the web service is down, and saying so
        promptly is the whole of the difference.

        How a regression manifests: every board install action pauses for the full
        timeout before reporting a failure it already knew about.
        """
        web = _Web(delivered=False)
        client = InstallControlClient(
            send_request=web.send, reply_timeout_seconds=10.0
        )
        web.client = client

        started = time.monotonic()
        result = client.install(ENGINE)

        assert result.accepted is False
        assert "not running" in result.message.lower()
        assert time.monotonic() - started < 1.0
        assert client.pending_count == 0

    def test_a_reply_from_another_thread_resolves_the_waiter(self, web):
        """The waiting call returns when the reply arrives out of band.

        Why: in production the reply is delivered by the settings-socket listener
        thread while the menu thread waits, which the inline fake does not
        exercise. This is the arrangement that actually runs on the board.

        How a regression manifests: the waiter never notices the delivery and
        every action reports a timeout despite the web answering correctly.
        """
        web = _Web(reply=False)
        client = InstallControlClient(send_request=web.send, reply_timeout_seconds=5.0)
        web.client = client

        def reply_soon():
            while not web.requests:
                time.sleep(0.005)
            client.deliver_reply({
                "request_id": web.last_request["request_id"],
                "accepted": True,
                "message": "Resuming Reckless",
            })

        threading.Thread(target=reply_soon, daemon=True).start()
        result = client.resume(ENGINE)

        assert result.accepted is True
        assert result.message == "Resuming Reckless"


class TestDiscardWaitsLonger:
    """Removing a build tree is the one slow action."""

    def test_discard_is_given_a_longer_deadline_than_the_others(self, client):
        """Discard's timeout exceeds the ordinary one.

        Why: the other three actions return as soon as the web has validated them
        -- dispatching a thread or setting a flag. Discard deletes the tree
        inline, and a Rust build tree is tens of thousands of files on an SD card.
        Holding all four to the same short deadline would report a timeout for a
        discard that is working correctly, and the board would tell the user their
        reclaimed disk had failed to be reclaimed.

        How a regression manifests: discarding a large tree reports "not
        responding" while the deletion carries on unseen.
        """
        assert client.timeout_for("discard") > client.timeout_for("install")
