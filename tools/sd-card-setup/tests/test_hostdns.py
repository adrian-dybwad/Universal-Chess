"""Tests for host DNS listener analysis.

The fixtures below are verbatim output captured from real machines during the
investigation that motivated this tool -- a macOS host sharing to a Pi Zero, and
a Debian Pi Zero. They are not written from memory, because two details in them
are exactly what a remembered fixture gets wrong: macOS truncates long IPv6
addresses mid-string, and Linux `ss` runs its header columns together as
"Peer Address:PortProcess".

The fault being modelled: a resolver binds one socket per address at startup and
never re-enumerates, so the USB bridge -- which cannot exist until the Pi is
plugged in -- is never bound, while DHCP still advertises it to the board.
"""

from __future__ import annotations

import hostdns
import pytest

# Captured on macOS 15.7.7 while the fault was present. dnsmasq had been running
# 13 days and was bound to 192.168.14.106, an address the machine no longer had;
# nothing was bound to the bridge at 192.168.2.1.
MACOS_NETSTAT_BROKEN = """\
udp6       0      0  ::1.53                 *.*
udp6       0      0  fe80::1%lo0.53         *.*
udp6       0      0  fe80::18f3:f818:.53    *.*
udp4       0      0  127.0.0.1.53           *.*
udp4       0      0  192.168.14.106.53      *.*
"""

# The same command after restarting the resolver, which re-enumerated and bound
# every current address including the bridge.
MACOS_NETSTAT_HEALTHY = """\
udp6       0      0  ::1.53                 *.*
udp6       0      0  fe80::603e:5fff:.53    *.*
udp4       0      0  127.0.0.1.53           *.*
udp4       0      0  172.20.10.4.53         *.*
udp4       0      0  192.168.2.1.53         *.*
"""

MACOS_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 172.20.10.4 netmask 0xfffffff0 broadcast 172.20.10.15
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 36:e7:80:b0:cb:40
bridge100: flags=8a63<UP,BROADCAST,SMART,RUNNING,ALLMULTI,SIMPLEX,MULTICAST> mtu 1500
\tether 62:3e:5f:34:36:64
\tinet 192.168.2.1 netmask 0xffffff00 broadcast 192.168.2.255
\tmember: en21 flags=3<LEARNING,DISCOVER>
\tstatus: active
"""

# Captured from the Pi. Note the header's run-together columns, which is why the
# parser keys on the UNCONN state column rather than skipping line one.
LINUX_SS = """\
State  Recv-Q Send-Q Local Address:Port  Peer Address:PortProcess
UNCONN 0      0            0.0.0.0:58940      0.0.0.0:*
UNCONN 0      0            0.0.0.0:5353       0.0.0.0:*
UNCONN 0      0                  *:47175            *:*
"""

LINUX_SS_WITH_DNS = """\
State  Recv-Q Send-Q Local Address:Port  Peer Address:PortProcess
UNCONN 0      0            0.0.0.0:5353       0.0.0.0:*
UNCONN 0      0         10.42.0.1:53          0.0.0.0:*    users:(("dnsmasq",pid=1421,fd=6))
"""

LINUX_IP_ADDR = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000
    inet 127.0.0.1/8 scope host lo
       valid_lft forever preferred_lft forever
2: usb0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP group default
    inet 192.168.2.4/24 brd 192.168.2.255 scope global dynamic noprefixroute usb0
       valid_lft 2382sec preferred_lft 2382sec
"""

WINDOWS_NETSTAT = """\
Active Connections

  Proto  Local Address          Foreign Address        PID
  UDP    0.0.0.0:53             *:*                    1234
  UDP    192.168.137.1:53       *:*                    1234
  UDP    10.0.0.5:5353          *:*                    900
"""

BRIDGE_PATTERNS = ("bridge",)


class TestBsdListenerParsing:
    def test_reads_ipv4_listeners_from_real_broken_output(self):
        """The captured fault must parse to exactly its two IPv4 listeners.

        Why this test exists: this is the output that started the whole
        investigation. Parsing it correctly -- and in particular *not* finding
        192.168.2.1 -- is the behaviour the tool exists to detect.

        How the regression manifests: a different count, or the stale
        192.168.14.106 going missing, which would make the fault look healthy.
        """
        listeners = hostdns.parse_bsd_listeners(MACOS_NETSTAT_BROKEN)

        assert [listener.address for listener in listeners] == [
            "127.0.0.1",
            "192.168.14.106",
        ]

    def test_skips_truncated_ipv6_rows(self):
        """Truncated IPv6 addresses must not become bogus listeners.

        Why this test exists: macOS clips the address column, producing rows
        like "fe80::18f3:f818:.53". Splitting that on the last dot yields the
        nonsense address "fe80::18f3:f818:", and admitting it could make a
        wildcard or shared-address match succeed by accident.

        How the regression manifests: any non-dotted-quad address in the result.
        """
        listeners = hostdns.parse_bsd_listeners(MACOS_NETSTAT_BROKEN)

        assert all(hostdns.is_ipv4(listener.address) for listener in listeners)

    def test_reads_the_bridge_address_when_present(self):
        """Post-fix output must show the bridge as bound.

        Why this test exists: it is the other half of the pair above. A parser
        that never returns the bridge address would report the fault as
        unfixable forever.

        How the regression manifests: 192.168.2.1 absent after the fix.
        """
        listeners = hostdns.parse_bsd_listeners(MACOS_NETSTAT_HEALTHY)

        assert "192.168.2.1" in [listener.address for listener in listeners]

    def test_ignores_ports_that_merely_end_in_53(self):
        """Port 5353 must not be mistaken for port 53. This is the near-miss case.

        Why this test exists: mDNS runs on 5353 and is present on virtually
        every machine. A substring or endswith match would treat it as a DNS
        listener and report a broken host as healthy.

        How the regression manifests: a listener appears from a 5353 row.
        """
        text = "udp4       0      0  192.168.2.1.5353       *.*\n"

        assert hostdns.parse_bsd_listeners(text) == ()

    def test_empty_input_yields_no_listeners(self):
        """The empty case must be quiet rather than raising."""
        assert hostdns.parse_bsd_listeners("") == ()


class TestSsListenerParsing:
    def test_header_row_is_not_parsed_as_a_listener(self):
        """The `ss` header must never become a listener.

        Why this test exists: the header reads "Peer Address:PortProcess" with
        columns run together, so a parser that skips line one by index or splits
        on whitespace can produce a phantom entry from it.

        How the regression manifests: a listener with a junk address.
        """
        listeners = hostdns.parse_ss_listeners(LINUX_SS)

        assert listeners == ()

    def test_extracts_address_and_owning_process(self):
        """A root-run `ss` must yield the address, pid and process name.

        Why this test exists: naming the owning process is what lets the user
        restart the right thing. Today's fault was misattributed to
        mDNSResponder for several rounds precisely because the owner was
        unknown.

        How the regression manifests: the listener parses but pid or process is
        None, degrading the report to a guess.
        """
        listeners = hostdns.parse_ss_listeners(LINUX_SS_WITH_DNS)

        assert len(listeners) == 1
        assert listeners[0] == hostdns.Listener(address="10.42.0.1", pid=1421, process="dnsmasq")

    def test_normalises_the_star_wildcard(self):
        """`*:53` and `0.0.0.0:53` both mean every address.

        Why this test exists: `ss` prints either depending on address family.
        Leaving `*` unnormalised makes the wildcard check in diagnose() miss,
        reporting a healthy wildcard-bound resolver as broken.

        How the regression manifests: a listener with address "*".
        """
        text = "UNCONN 0      0                  *:53            *:*\n"
        listeners = hostdns.parse_ss_listeners(text)

        assert listeners[0].address == hostdns.WILDCARD_ADDRESS

    def test_missing_process_column_is_not_an_error(self):
        """Without root, `ss` omits the process field and that is fine.

        Why this test exists: the tool must still diagnose when run unprivileged;
        it just cannot name the owner. Treating the absence as a parse failure
        would block the diagnosis entirely.

        How the regression manifests: an exception, or the listener being
        dropped.
        """
        text = "UNCONN 0      0         10.42.0.1:53          0.0.0.0:*\n"
        listeners = hostdns.parse_ss_listeners(text)

        assert listeners[0].address == "10.42.0.1"
        assert listeners[0].pid is None


class TestWindowsListenerParsing:
    def test_extracts_listeners_and_pids(self):
        """Documented ICS output must parse to its two port-53 rows.

        Why this test exists: Windows is diagnosed but never auto-fixed, so the
        report is the entire value there and must at least be correct about what
        is bound.

        How the regression manifests: the 5353 row leaking in, or a missing pid.
        """
        listeners = hostdns.parse_windows_listeners(WINDOWS_NETSTAT)

        assert [(x.address, x.pid) for x in listeners] == [
            (hostdns.WILDCARD_ADDRESS, 1234),
            ("192.168.137.1", 1234),
        ]


class TestParseFailureDetection:
    def test_flags_output_that_mentions_port_53_but_did_not_parse(self):
        """An uninterpretable format must not read as "nothing is listening".

        Why this test exists: the Windows parser is the one format never
        validated against a real machine. If it silently returns nothing, the
        tool would diagnose NO_RESOLVER and send the user to fix a fault that is
        not there.

        How the regression manifests: parse_failed returns False for output that
        clearly references port 53.
        """
        raw = "  UDP    0.0.0.0:53    *:*    1234\n"

        assert hostdns.parse_failed(raw, ()) is True

    def test_does_not_flag_genuinely_empty_output(self):
        """Output with no port-53 mention is a real absence, not a parse failure.

        How the regression manifests: every clean host reported as unparseable.
        """
        assert hostdns.parse_failed("UDP 0.0.0.0:5353 *:* 900\n", ()) is False

    def test_does_not_flag_successful_parses(self):
        """Any parsed listener means the format was understood."""
        assert hostdns.parse_failed("anything", (hostdns.Listener("1.2.3.4"),)) is False


class TestInterfaceParsing:
    def test_finds_the_sharing_bridge_and_skips_the_one_without_an_address(self):
        """bridge100 must be chosen over bridge0. This is the real ambiguity.

        Why this test exists: macOS hosts have a Thunderbolt bridge0 alongside
        the sharing bridge100. Hardcoding bridge100 is wrong (the number varies)
        and taking the first bridge is wrong (bridge0 comes first). The
        distinguishing property is having an IPv4 address.

        How the regression manifests: bridge0 selected, so the tool checks an
        address that does not exist and reports a healthy host as broken.
        """
        interfaces = hostdns.parse_bsd_interfaces(MACOS_IFCONFIG)
        shared = hostdns.find_shared_interface(interfaces, BRIDGE_PATTERNS)

        assert shared == hostdns.Interface(name="bridge100", address="192.168.2.1")

    def test_excludes_loopback_from_shared_candidates(self):
        """Loopback must never be treated as the shared link.

        Why this test exists: a resolver bound only to loopback is the failure
        being diagnosed. Selecting 127.0.0.1 as the shared address would make
        that exact fault report as healthy.

        How the regression manifests: an Interface on 127.0.0.1 returned.
        """
        interfaces = hostdns.parse_bsd_interfaces(MACOS_IFCONFIG)
        shared = hostdns.find_shared_interface(interfaces, ("lo",))

        assert shared is None

    def test_parses_ip_addr_output(self):
        """Linux interfaces and addresses must come through with prefixes stripped.

        Why this test exists: `ip` reports "192.168.2.4/24"; leaving the prefix
        on would make every address comparison fail silently.

        How the regression manifests: an address containing "/".
        """
        interfaces = hostdns.parse_ip_addr_interfaces(LINUX_IP_ADDR)

        assert hostdns.Interface(name="usb0", address="192.168.2.4") in interfaces
        assert all("/" not in i.address for i in interfaces)

    def test_returns_none_when_nothing_matches(self):
        """No sharing interface is a distinct, reportable state."""
        assert hostdns.find_shared_interface((), BRIDGE_PATTERNS) is None


SHARED = hostdns.Interface(name="bridge100", address="192.168.2.1")


class TestDiagnosis:
    @pytest.mark.parametrize(
        ("shared", "listeners", "expected"),
        [
            (None, (), hostdns.Diagnosis.NO_SHARED_INTERFACE),
            (
                None,
                (hostdns.Listener("192.168.2.1"),),
                hostdns.Diagnosis.NO_SHARED_INTERFACE,
            ),
            (SHARED, (), hostdns.Diagnosis.NO_RESOLVER),
            (
                SHARED,
                (hostdns.Listener("192.168.2.1"),),
                hostdns.Diagnosis.HEALTHY,
            ),
            (
                SHARED,
                (hostdns.Listener(hostdns.WILDCARD_ADDRESS),),
                hostdns.Diagnosis.HEALTHY,
            ),
            (
                SHARED,
                (hostdns.Listener("127.0.0.1"), hostdns.Listener("192.168.14.106")),
                hostdns.Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE,
            ),
        ],
        ids=[
            "no-sharing-and-no-listeners",
            "no-sharing-outranks-listeners",
            "sharing-up-but-nothing-listening",
            "bound-to-the-shared-address",
            "wildcard-covers-everything",
            "the-observed-fault",
        ],
    )
    def test_decision_table(self, shared, listeners, expected):
        """Each distinguishable state must map to its own diagnosis.

        Why this test exists: the four states need different responses -- start
        sharing, investigate a missing resolver, restart the owner, or do
        nothing. Collapsing any two sends users to the wrong fix, which is how
        several hours went into restarting the wrong process.

        How the regression manifests: a case returning a neighbouring diagnosis,
        most damagingly the observed fault reading as HEALTHY.
        """
        assert hostdns.diagnose(shared, listeners) == expected

    def test_wildcard_resolver_is_immune_to_the_fault(self):
        """A wildcard bind must read healthy even with no per-address socket.

        Why this test exists: a resolver on 0.0.0.0 answers on interfaces that
        appear after it started, so it genuinely cannot exhibit this fault.
        Reporting it as broken would have users restart a daemon needlessly.

        How the regression manifests: RESOLVER_MISSED_SHARED_INTERFACE for a
        wildcard-bound resolver.
        """
        result = hostdns.diagnose(SHARED, (hostdns.Listener(hostdns.WILDCARD_ADDRESS),))

        assert result == hostdns.Diagnosis.HEALTHY

    def test_real_captured_fault_diagnoses_as_the_fault(self):
        """End to end over real fixtures: broken input must diagnose as broken.

        Why this test exists: the unit pieces can each be right while the
        composition is wrong. This drives the actual captured output all the way
        through to a diagnosis.

        How the regression manifests: the captured fault reading as anything
        other than RESOLVER_MISSED_SHARED_INTERFACE.
        """
        interfaces = hostdns.parse_bsd_interfaces(MACOS_IFCONFIG)
        shared = hostdns.find_shared_interface(interfaces, BRIDGE_PATTERNS)
        listeners = hostdns.parse_bsd_listeners(MACOS_NETSTAT_BROKEN)

        assert hostdns.diagnose(shared, listeners) == (
            hostdns.Diagnosis.RESOLVER_MISSED_SHARED_INTERFACE
        )

    def test_real_captured_fix_diagnoses_as_healthy(self):
        """The same path over post-fix output must report healthy.

        Why this test exists: a tool that can never say "healthy" is useless for
        confirming a fix worked, and this is the exact output observed after the
        restart succeeded.

        How the regression manifests: HEALTHY never being reachable from real
        output.
        """
        interfaces = hostdns.parse_bsd_interfaces(MACOS_IFCONFIG)
        shared = hostdns.find_shared_interface(interfaces, BRIDGE_PATTERNS)
        listeners = hostdns.parse_bsd_listeners(MACOS_NETSTAT_HEALTHY)

        assert hostdns.diagnose(shared, listeners) == hostdns.Diagnosis.HEALTHY
