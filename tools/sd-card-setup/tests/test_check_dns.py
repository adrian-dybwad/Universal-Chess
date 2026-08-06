"""Tests for the host-side DNS repair tool.

These concentrate on the safety boundaries rather than the parsing, which is
covered in test_hostdns.py against real captured output. The boundaries matter
because this tool restarts system daemons: it must never act on a platform where
the remedy is unproven, never restart something nothing will bring back, and
never act without consent.

Every external command is injected, so no test touches the real host.
"""

from __future__ import annotations

import enable_usb_gadget as cli
import hostcheck
import pytest
from hostdns import Diagnosis

# Real macOS output, trimmed. dnsmasq bound to a dead address and loopback, with
# nothing on the 192.168.2.1 bridge.
NETSTAT_BROKEN = """\
udp4       0      0  127.0.0.1.53           *.*
udp4       0      0  192.168.14.106.53      *.*
"""

NETSTAT_HEALTHY = """\
udp4       0      0  127.0.0.1.53           *.*
udp4       0      0  192.168.2.1.53         *.*
"""

IFCONFIG = """\
bridge0: flags=8863<UP> mtu 1500
\tether 36:e7:80:b0:cb:40
bridge100: flags=8a63<UP> mtu 1500
\tinet 192.168.2.1 netmask 0xffffff00 broadcast 192.168.2.255
"""

# lsof truncates COMMAND to nine characters by default; the tool passes +c 0 to
# prevent that, and keys the restart on pid regardless.
LSOF = """\
COMMAND     PID           USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
dnsmasq   48461         nobody    4u  IPv4 0x8b062f87e4378000      0t0  UDP 127.0.0.1:53
dnsmasq   48461         nobody    5u  IPv4 0x57561faa6e765b30      0t0  UDP 192.168.14.106:53
"""

SUPERVISED_PID = "48461"


class FakeHost:
    """Answers injected commands from a table and records what was run."""

    def __init__(self, responses, ppid="1"):
        self._responses = responses
        self._ppid = ppid
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command):
        command = tuple(command)
        self.commands.append(command)
        if command[0] == "ps":
            return f"  {self._ppid}\n"
        for prefix, output in self._responses.items():
            if command[: len(prefix)] == prefix:
                return output
        return ""

    @property
    def ran_a_kill(self) -> bool:
        return any("kill" in c for c in self.commands)


def broken_host(**kwargs) -> FakeHost:
    return FakeHost(
        {
            ("netstat",): NETSTAT_BROKEN,
            ("ifconfig",): IFCONFIG,
            ("sudo", "lsof"): LSOF,
        },
        **kwargs,
    )


@pytest.fixture
def on_macos(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")


def _check(args, run):
    """Drive the DNS check through the single tool's --check-dns mode.

    The check used to be its own script. Routing every case through the flag
    keeps these tests on the command users actually run, so a regression in the
    argument wiring shows up here rather than only in the field.
    """
    return cli.main(["--check-dns", *args], run=run)


class TestPlatformDetection:
    @pytest.mark.parametrize(
        ("sys_platform", "expected"),
        [
            ("darwin", "macos"),
            ("linux", "linux"),
            ("linux2", "linux"),
            ("win32", "windows"),
            ("freebsd14", None),
        ],
    )
    def test_maps_sys_platform_to_support(self, sys_platform, expected):
        """Each supported host must resolve to its own support entry.

        Why this test exists: the platform key selects which commands run and
        whether repair is permitted at all. Mapping Linux to the macOS entry
        would run a macOS-only remedy on Linux.

        How the regression manifests: a wrong or missing key, most seriously an
        unknown platform resolving to something rather than None.
        """
        assert hostcheck.detect_platform(sys_platform) == expected


@pytest.mark.usefixtures("on_macos")
class TestDiagnosisOnly:
    def test_reports_the_fault_without_touching_anything(self, capsys):
        """Without --fix the tool must diagnose and change nothing.

        Why this test exists: this is the default invocation. A tool that
        restarts daemons merely for being run is unusable, and the existing card
        tool sets the precedent that acting requires an explicit flag.

        How the regression manifests: any command beyond inspection is run, or
        the fault is not reported.
        """
        host = broken_host()
        code = _check([], run=host)
        out = capsys.readouterr().out

        assert Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE.value in out
        assert "--fix" in out
        assert code == 1
        assert not host.ran_a_kill
        assert not any(c[0] == "sudo" for c in host.commands)

    def test_reports_healthy_and_exits_zero(self, capsys):
        """A working host must exit 0 and propose nothing.

        Why this test exists: the tool is also how a user confirms a fix worked.
        If it cannot report success, it cannot close the loop.

        How the regression manifests: a healthy host exiting non-zero or being
        offered a repair.
        """
        host = FakeHost({("netstat",): NETSTAT_HEALTHY, ("ifconfig",): IFCONFIG})
        code = _check(["--fix"], run=host)
        out = capsys.readouterr().out

        assert code == 0
        assert Diagnosis.HEALTHY.value in out
        assert not host.ran_a_kill

    def test_identifies_the_bridge_and_ignores_the_addressless_one(self, capsys):
        """bridge100 must be named in the report, not bridge0.

        Why this test exists: picking bridge0 would make the tool check an
        address that does not exist and declare a healthy host broken.

        How the regression manifests: bridge0 named, or no shared link found.
        """
        _check([], run=broken_host())
        out = capsys.readouterr().out

        assert "bridge100 at 192.168.2.1" in out

    def test_shared_address_override_skips_detection(self, capsys):
        """An explicit address must be used verbatim.

        Why this test exists: detection is prefix-based and will not recognise
        every host's sharing setup. Without an override those users have no way
        to run the tool at all.

        How the regression manifests: the override ignored, or ifconfig still
        consulted to override it.
        """
        host = FakeHost({("netstat",): NETSTAT_BROKEN})
        _check(["--shared-address", "10.42.0.1"], run=host)
        out = capsys.readouterr().out

        assert "10.42.0.1" in out
        assert Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE.value in out


@pytest.mark.usefixtures("on_macos")
class TestRepairSafety:
    def test_refuses_to_restart_an_unsupervised_process(self, capsys):
        """An unsupervised resolver must never be killed. This is the worst case.

        Why this test exists: killing a resolver that nothing relaunches leaves
        the host with no DNS whatsoever -- strictly worse than the fault being
        repaired, and inflicted by the tool meant to fix it.

        How the regression manifests: a kill is issued for a process whose
        parent is not pid 1.
        """
        host = broken_host(ppid="4821")
        code = _check(["--yes", "--fix"], run=host)
        out = capsys.readouterr().out

        assert not host.ran_a_kill
        assert "no DNS at all" in out
        assert code == 1

    def test_restarts_a_supervised_process_by_pid(self, capsys):
        """A launchd-supervised resolver must be restarted by pid.

        Why this test exists: lsof truncates COMMAND to nine characters, so a
        name-based killall would silently miss any longer daemon name. The pid
        is unambiguous.

        How the regression manifests: the kill targets a name, or the wrong pid.
        """
        host = broken_host()
        code = _check(["--yes", "--fix"], run=host)
        out = capsys.readouterr().out

        assert ("sudo", "kill", SUPERVISED_PID) in host.commands
        assert "dnsmasq" in out
        assert code == 0

    def test_requires_confirmation_before_the_privileged_lookup(self, capsys, monkeypatch):
        """Declining the first prompt must stop before any sudo command.

        Why this test exists: identifying the owner needs elevated privileges.
        Escalating without asking, on a machine the user only wanted diagnosed,
        breaks the tool's contract.

        How the regression manifests: a sudo command in the log after the user
        said no.
        """
        monkeypatch.setattr(hostcheck, "confirm", lambda _prompt: False)
        host = broken_host()
        code = _check(["--fix"], run=host)

        assert not any(c[0] == "sudo" for c in host.commands)
        assert "Nothing was changed" in capsys.readouterr().out
        assert code == 1

    def test_declining_the_restart_prompt_changes_nothing(self, capsys, monkeypatch):
        """Consenting to look must not imply consenting to restart.

        Why this test exists: these are separate decisions -- one reads state,
        the other interrupts a running system service. Collapsing them means a
        user who agreed to a diagnostic gets a daemon restarted.

        How the regression manifests: a kill after only the first prompt was
        accepted.
        """
        answers = iter([True, False])
        monkeypatch.setattr(hostcheck, "confirm", lambda _prompt: next(answers))
        host = broken_host()
        code = _check(["--fix"], run=host)

        assert not host.ran_a_kill
        assert "Nothing was changed" in capsys.readouterr().out
        assert code == 1

    def test_never_acts_on_a_platform_without_a_verified_remedy(self, monkeypatch, capsys):
        """Linux and Windows must report the command, never run it.

        Why this test exists: the scope of this tool is automation only where
        the fix was observed to work. During the investigation three plausible
        macOS remedies did nothing; shipping an untested Linux equivalent that
        restarts a daemon repeats that mistake with real consequences.

        How the regression manifests: restart_command becoming non-None for an
        unverified platform, so --yes issues a real command.
        """
        monkeypatch.setattr(cli.sys, "platform", "linux")
        host = FakeHost(
            {
                ("ss",): "UNCONN 0 0 127.0.0.1:53 0.0.0.0:*\n",
                ("ip",): ("2: usb0: <UP> mtu 1500\n    inet 192.168.2.1/24 scope global usb0\n"),
            }
        )
        code = _check(["--yes", "--fix"], run=host)
        out = capsys.readouterr().out

        assert not host.ran_a_kill
        assert "has been verified" in out
        assert code == 1


@pytest.mark.usefixtures("on_macos")
class TestParseFailureReporting:
    def test_warns_when_output_could_not_be_interpreted(self, capsys):
        """Unrecognised output must be flagged, not read as "nothing listening".

        Why this test exists: a parser that quietly returns nothing would
        diagnose NO_RESOLVER on a host that is running one, sending the user to
        chase a fault that does not exist. This matters most on Windows, the one
        format never validated against a real machine.

        How the regression manifests: no warning, and a confident wrong
        diagnosis from output the tool did not understand.
        """
        host = FakeHost(
            {
                ("netstat",): "some.unexpected.format:53 listening\n",
                ("ifconfig",): IFCONFIG,
            }
        )
        _check([], run=host)
        out = capsys.readouterr().out

        assert "could not be" in out
        assert "may be wrong" in out


class TestLsofParsing:
    def test_maps_addresses_to_the_owning_process(self):
        """Each bound address must resolve to its pid and command.

        Why this test exists: naming the owner is the step that ended a long
        misdiagnosis, where the wrong daemon was restarted repeatedly because
        nobody had checked who actually held the socket.

        How the regression manifests: an empty map, or the header row parsed as
        an entry.
        """
        owners = hostcheck.parse_lsof_owners(LSOF)

        assert owners == {
            "127.0.0.1": (48461, "dnsmasq"),
            "192.168.14.106": (48461, "dnsmasq"),
        }

    def test_skips_connected_sockets_and_ipv6(self):
        """Only plain IPv4 listening endpoints count.

        Why this test exists: lsof also lists connected pairs as
        "local->remote" and bracketed IPv6. Splitting those on the last colon
        yields a nonsense address that could be mistaken for a listener.

        How the regression manifests: any entry from a -> or [ ] row.
        """
        text = (
            "Google    70095 me   39u  IPv6 0x1  0t0  UDP [2607::1]:63336->[2607::2]:443\n"
            "chrome    70095 me   40u  IPv4 0x2  0t0  UDP 10.0.0.5:53->1.1.1.1:53\n"
        )

        assert hostcheck.parse_lsof_owners(text) == {}
