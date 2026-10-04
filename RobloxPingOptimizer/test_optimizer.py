"""Unit tests for roblox_wifi_ping_optimizer.

Pure logic only: no network, no hosts writes, no external dependencies.
Run with:  python -m unittest -v test_optimizer
"""

import sys
import unittest

import roblox_wifi_ping_optimizer as opt


def make_entry(*rounds):
    """Build a scan entry from (best, ms, jitter) tuples, one per round."""
    return {
        "host": opt.PRIORITY_HOST,
        "history": [{"best": b, "ms": m, "jitter": j, "ts": float(i)}
                    for i, (b, m, j) in enumerate(rounds)],
    }


A, B, C = "104.18.32.47", "104.18.33.47", "172.64.32.10"


class StatsTests(unittest.TestCase):
    def test_pct_median(self):
        self.assertEqual(opt.pct([1, 2, 3, 4, 5], 0.5), 3)

    def test_pct_sorts_input(self):
        self.assertEqual(opt.pct([5, 1, 4, 2, 3], 0.5), 3)

    def test_pct_p95(self):
        self.assertEqual(opt.pct([1, 2, 3, 4, 5], 0.95), 5)

    def test_pct_single_sample(self):
        self.assertEqual(opt.pct([10], 0.95), 10)

    def test_pct_empty_is_none(self):
        self.assertIsNone(opt.pct([], 0.5))

    def test_jitter_mean_of_recent_diffs(self):
        self.assertEqual(opt.jitter([10, 12, 11]), 1.5)

    def test_jitter_tiny_samples_are_zero(self):
        self.assertEqual(opt.jitter([]), 0)
        self.assertEqual(opt.jitter([7]), 0)

    def test_jitter_only_looks_at_last_five_samples(self):
        # Two wild swings live outside the 5-sample window.
        self.assertEqual(opt.jitter([100, 100, 100, 100, 100, 0, 100]), 50.0)


class StabilityGateTests(unittest.TestCase):
    def test_agreed_rounds_under_sla_pass(self):
        ok, reason = opt.stability_gate(
            make_entry((A, 40, 5), (A, 41, 6), (A, 39, 4)))
        self.assertTrue(ok, reason)
        self.assertIn(A, reason)

    def test_majority_within_last_three_rounds(self):
        ok, reason = opt.stability_gate(
            make_entry((A, 40, 5), (A, 41, 5), (B, 44, 5), (A, 42, 5)))
        self.assertTrue(ok, reason)

    def test_single_round_can_pass(self):
        # --rounds 1 used to be permanently ineligible.
        ok, reason = opt.stability_gate(make_entry((A, 40, 5)))
        self.assertTrue(ok, reason)

    def test_sla_breach_rejected(self):
        ok, reason = opt.stability_gate(make_entry((A, 50, 5)))
        self.assertFalse(ok)
        self.assertIn("SLA", reason)

    def test_high_jitter_rejected(self):
        ok, reason = opt.stability_gate(make_entry((A, 40, 16)))
        self.assertFalse(ok)
        self.assertIn("jitter", reason)

    def test_three_way_flap_rejected(self):
        ok, reason = opt.stability_gate(
            make_entry((A, 40, 5), (B, 40, 5), (C, 40, 5)))
        self.assertFalse(ok)
        self.assertIn("flapped", reason)

    def test_latest_round_must_match_winner(self):
        ok, reason = opt.stability_gate(
            make_entry((A, 40, 5), (A, 41, 5), (B, 42, 5)))
        self.assertFalse(ok)
        self.assertIn("differs", reason)

    def test_dead_round_never_counts_as_agreement(self):
        ok, reason = opt.stability_gate(
            make_entry((A, 40, 5), (A, 41, 5), (None, None, None)))
        self.assertFalse(ok)
        self.assertIn("no reachable edge", reason)

    def test_empty_history(self):
        ok, reason = opt.stability_gate({"history": []})
        self.assertFalse(ok)
        self.assertEqual(reason, "no history")


TRACERT_SAMPLE = """\
Tracing route to setup.roblox.com [104.18.32.47]
over a maximum of 15 hops:

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2     8 ms     9 ms     8 ms  10.0.0.1
  3     *        *        *     Request timed out.
  4    15 ms    14 ms    16 ms  104.18.32.47

Trace complete.
"""


class TracertParseTests(unittest.TestCase):
    def setUp(self):
        self.hops = opt.parse_tracert_output(TRACERT_SAMPLE)

    def test_only_numbered_hop_lines_are_parsed(self):
        self.assertEqual([h["hop"] for h in self.hops], [1, 2, 3, 4])

    def test_sub_millisecond_rounds_to_zero(self):
        self.assertEqual(self.hops[0]["avg_ms"], 0)
        self.assertEqual(self.hops[0]["ip"], "192.168.1.1")

    def test_hop_times_are_averaged(self):
        self.assertEqual(self.hops[1]["avg_ms"], 8)
        self.assertEqual(self.hops[1]["ip"], "10.0.0.1")

    def test_timed_out_hop_has_no_average(self):
        self.assertIsNone(self.hops[2]["avg_ms"])
        self.assertEqual(self.hops[2]["ip"], "?")

    def test_last_hop_parsed(self):
        self.assertEqual(self.hops[3]["avg_ms"], 15)
        self.assertEqual(self.hops[3]["ip"], "104.18.32.47")


class BottleneckTests(unittest.TestCase):
    def test_finds_largest_jump(self):
        hops = [
            {"hop": 1, "ip": "192.168.1.1", "avg_ms": 5},
            {"hop": 2, "ip": "10.0.0.1", "avg_ms": 40},
            {"hop": 3, "ip": "9.9.9.9", "avg_ms": 41},
        ]
        self.assertEqual(opt.identify_bottleneck(hops), (2, 5, 35, "10.0.0.1"))

    def test_flat_path_has_no_bottleneck(self):
        hops = [
            {"hop": 1, "ip": "192.168.1.1", "avg_ms": 5},
            {"hop": 2, "ip": "10.0.0.1", "avg_ms": 8},
        ]
        self.assertIsNone(opt.identify_bottleneck(hops))

    def test_unknown_hops_are_skipped(self):
        hops = [
            {"hop": 1, "ip": "192.168.1.1", "avg_ms": None},
            {"hop": 2, "ip": "10.0.0.1", "avg_ms": 40},
        ]
        self.assertIsNone(opt.identify_bottleneck(hops))


HOSTS_EXISTING = [
    "# Copyright (c) Microsoft Corp.",
    "127.0.0.1       localhost",
    "::1             localhost",
    "10.0.0.1 setup.roblox.com",                      # stale, no marker
    "# Roblox Setup Optimizer Start",
    "9.9.9.9\tsetup.roblox.com\t# Roblox Setup Optimizer",
    "# Roblox Setup Optimizer End",
    "1.1.1.1 api.roblox.com",
    "# note about setup.roblox.com stays a comment",
]


class HostsPruneTests(unittest.TestCase):
    def setUp(self):
        self.out = opt.prune_hosts_lines(HOSTS_EXISTING, {"setup.roblox.com"})

    def test_marker_block_removed(self):
        self.assertFalse(any(opt.HOSTS_MARKER in line for line in self.out))

    def test_stale_unmarked_entry_removed(self):
        self.assertNotIn("10.0.0.1 setup.roblox.com", self.out)

    def test_unrelated_entries_survive(self):
        self.assertIn("# Copyright (c) Microsoft Corp.", self.out)
        self.assertIn("127.0.0.1       localhost", self.out)
        self.assertIn("1.1.1.1 api.roblox.com", self.out)

    def test_comments_survive(self):
        self.assertIn("# note about setup.roblox.com stays a comment", self.out)


class HostsBuildTests(unittest.TestCase):
    ENTRY = "104.18.32.47\tsetup.roblox.com\t# Roblox Setup Optimizer"
    START = "# Roblox Setup Optimizer Start"
    END = "# Roblox Setup Optimizer End"

    def setUp(self):
        self.lines = opt.build_hosts_lines(
            HOSTS_EXISTING, {"setup.roblox.com": "104.18.32.47"})

    def test_managed_block_written(self):
        self.assertIn(self.START, self.lines)
        self.assertIn(self.ENTRY, self.lines)
        self.assertIn(self.END, self.lines)

    def test_block_is_ordered_start_entry_end(self):
        start = self.lines.index(self.START)
        entry = self.lines.index(self.ENTRY)
        end = self.lines.index(self.END)
        self.assertTrue(start < entry < end)

    def test_old_block_replaced_not_duplicated(self):
        self.assertEqual(
            sum(1 for l in self.lines if l == self.START), 1)
        self.assertNotIn(
            "9.9.9.9\tsetup.roblox.com\t# Roblox Setup Optimizer", self.lines)

    def test_other_hosts_kept(self):
        self.assertIn("1.1.1.1 api.roblox.com", self.lines)
        self.assertIn("127.0.0.1       localhost", self.lines)


class ParseTargetsTests(unittest.TestCase):
    def test_hosts_and_ports(self):
        self.assertEqual(
            opt.parse_targets("setup.roblox.com,rbxgame.roblox.com:8443"),
            [("setup.roblox.com", 443, "setup.roblox.com"),
             ("rbxgame.roblox.com", 8443, "rbxgame.roblox.com")])

    def test_whitespace_and_blank_entries_ignored(self):
        self.assertEqual(
            opt.parse_targets("  setup.roblox.com , , "),
            [("setup.roblox.com", 443, "setup.roblox.com")])

    def test_trailing_dot_forgiven(self):
        self.assertEqual(opt.parse_targets("setup.roblox.com.")[-1][0],
                         "setup.roblox.com")

    def test_ipv6_literal_keeps_default_port(self):
        self.assertEqual(opt.parse_targets("2001:db8::1"),
                         [("2001:db8::1", 443, "2001:db8::1")])

    def test_rejects_shell_metacharacters(self):
        for bad in ("evil.com;calc", "host && whoami", "a b.com",
                    "host$(id)", "$(reboot).com", "host|nc", "x&y"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    opt.parse_targets(bad)

    def test_rejects_bad_ports(self):
        for bad in ("host:notaport", "host:70000", "host:0", "host:-1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    opt.parse_targets(bad)

    def test_rejects_empty_spec(self):
        with self.assertRaises(ValueError):
            opt.parse_targets(" , ")


class DoHAnswerTests(unittest.TestCase):
    def test_extracts_a_records_only(self):
        data = {"Answer": [
            {"type": 1, "data": "1.2.3.4"},
            {"type": 5, "data": "cdn.example.com"},
            {"type": 1, "data": "not-an-ip"},
        ]}
        self.assertEqual(opt._parse_doh_answers(data, 1), ["1.2.3.4"])

    def test_extracts_aaaa_and_strips_zone_id(self):
        data = {"Answer": [
            {"type": 28, "data": "2001:db8::1%eth0"},
            {"type": 28, "data": "fe80::1"},
        ]}
        self.assertEqual(opt._parse_doh_answers(data, 28),
                         ["2001:db8::1", "fe80::1"])

    def test_missing_or_empty_answer_section(self):
        self.assertEqual(opt._parse_doh_answers({}, 1), [])
        self.assertEqual(opt._parse_doh_answers({"Answer": []}, 1), [])
        self.assertEqual(opt._parse_doh_answers(None, 28), [])


class ParserTests(unittest.TestCase):
    def parse(self, argv):
        return opt.build_parser().parse_args(argv)

    def test_quick_and_aggressive_flags_exist(self):
        args = self.parse(["--quick", "--aggressive"])
        self.assertTrue(args.quick)
        self.assertTrue(args.aggressive)

    def test_short_flags(self):
        args = self.parse(["-m", "10", "-i", "15"])
        self.assertEqual(args.minutes, 10)
        self.assertEqual(args.interval, 15)

    def test_defaults_left_unset_for_session_resolution(self):
        args = self.parse([])
        self.assertIsNone(args.minutes)
        self.assertIsNone(args.rounds)
        self.assertFalse(args.quick)
        self.assertFalse(args.aggressive)
        self.assertFalse(args.apply_hosts)
        self.assertFalse(args.scan_only)


class ResolveSessionTests(unittest.TestCase):
    def args(self, argv):
        return opt.build_parser().parse_args(argv)

    def test_plain_defaults(self):
        self.assertEqual(opt.resolve_session(self.args([])), (30, 3))

    def test_quick_defaults(self):
        self.assertEqual(opt.resolve_session(self.args(["--quick"])), (5, 1))

    def test_explicit_minutes_beats_quick(self):
        self.assertEqual(
            opt.resolve_session(self.args(["--quick", "-m", "60"])), (60, 1))

    def test_explicit_rounds_beats_quick(self):
        self.assertEqual(
            opt.resolve_session(self.args(["--quick", "--rounds", "4"])), (5, 4))

    def test_rejects_zero_minutes(self):
        with self.assertRaises(ValueError):
            opt.resolve_session(self.args(["-m", "0"]))

    def test_rejects_zero_rounds(self):
        with self.assertRaises(ValueError):
            opt.resolve_session(self.args(["--rounds", "0"]))


class RunHelperTests(unittest.TestCase):
    """run() must never hand strings to a shell."""

    def test_executes_argv_list(self):
        rc, out, err = opt.run([sys.executable, "-c", "print('ok')"],
                               timeout=30)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "ok")

    def test_arguments_pass_through_verbatim(self):
        rc, out, err = opt.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])",
             "a&b|c>d"], timeout=30)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "a&b|c>d")

    def test_missing_binary_reports_failure(self):
        rc, out, err = opt.run(["no-such-binary-xyz-123"], timeout=10)
        self.assertEqual(rc, -1)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
