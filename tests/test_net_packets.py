"""Unit tests for the pure packet-rate data layer (no subprocess, no AppKit)."""

from claude_swap import net_packets


# ---- parse_nettop_sample ------------------------------------------------

def test_parse_extracts_pid_and_counters():
    block = (
        "12:46:29.488417,kernel_task.0,1791010,3375865,\n"
        "12:46:29.488420,apsd.565,890,870,\n"
    )
    assert net_packets.parse_nettop_sample(block) == {
        0: (1791010, 3375865),
        565: (890, 870),
    }


def test_parse_skips_header_line():
    block = "time,,packets_in,packets_out,\n12:46:29.4,node.123,10,20,\n"
    assert net_packets.parse_nettop_sample(block) == {123: (10, 20)}


def test_parse_pid_is_integer_after_last_dot():
    # Process names can contain dots; the pid is the trailing .<digits>.
    block = "12:46:29.4,com.apple.WebKit.128,5,6,\n"
    assert net_packets.parse_nettop_sample(block) == {128: (5, 6)}


def test_parse_skips_junk_and_short_rows():
    block = (
        "garbage\n"
        ",,,\n"
        "12:46:29.4,noPidHere,1,2,\n"        # second field has no .pid
        "12:46:29.4,proc.notanumber,1,2,\n"  # pid not numeric
        "12:46:29.4,ok.7,3,4,\n"
    )
    assert net_packets.parse_nettop_sample(block) == {7: (3, 4)}


def test_parse_empty_block_is_empty_dict():
    assert net_packets.parse_nettop_sample("") == {}


# ---- sum_targets --------------------------------------------------------

def test_sum_targets_adds_in_and_out_for_present_pids():
    sample = {1: (10, 5), 2: (100, 50), 3: (1, 1)}
    assert net_packets.sum_targets(sample, {1, 3}) == 10 + 5 + 1 + 1


def test_sum_targets_ignores_absent_pids():
    sample = {1: (10, 5)}
    assert net_packets.sum_targets(sample, {2, 3}) == 0


def test_sum_targets_empty_pidset_is_zero():
    assert net_packets.sum_targets({1: (10, 5)}, set()) == 0


# ---- delta_rate ---------------------------------------------------------

def test_delta_rate_first_sample_is_zero():
    assert net_packets.delta_rate(None, 500) == 0


def test_delta_rate_normal_positive_delta():
    assert net_packets.delta_rate(500, 650) == 150


def test_delta_rate_counter_reset_clamps_to_zero():
    # A target PID exited / counters reset -> cumulative drops -> no negative spike.
    assert net_packets.delta_rate(650, 100) == 0


# ---- normalize ----------------------------------------------------------

def test_normalize_empty_is_empty():
    assert net_packets.normalize([]) == []


def test_normalize_all_zero_is_all_zero():
    assert net_packets.normalize([0, 0, 0]) == [0.0, 0.0, 0.0]


def test_normalize_scales_against_peak():
    assert net_packets.normalize([0, 50, 100]) == [0.0, 0.5, 1.0]
