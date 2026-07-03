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


# ---- scroll_fraction ----------------------------------------------------

def test_scroll_fraction_none_last_sample_is_zero():
    assert net_packets.scroll_fraction(100.0, None, 1.0) == 0.0


def test_scroll_fraction_zero_interval_is_zero():
    assert net_packets.scroll_fraction(100.0, 99.0, 0.0) == 0.0


def test_scroll_fraction_just_after_sample_is_zero():
    assert net_packets.scroll_fraction(50.0, 50.0, 1.0) == 0.0


def test_scroll_fraction_halfway():
    assert net_packets.scroll_fraction(50.5, 50.0, 1.0) == 0.5


def test_scroll_fraction_clamps_to_one_when_sample_overdue():
    # A late next sample holds the scroll at the fully-advanced position.
    assert net_packets.scroll_fraction(52.0, 50.0, 1.0) == 1.0


def test_scroll_fraction_clamps_low_on_backwards_clock():
    assert net_packets.scroll_fraction(49.0, 50.0, 1.0) == 0.0


# ---- log2_scale ---------------------------------------------------------

def test_log2_scale_empty_is_empty():
    assert net_packets.log2_scale([]) == []


def test_log2_scale_zero_maps_to_zero():
    assert net_packets.log2_scale([0]) == [0.0]  # log2(1) == 0


def test_log2_scale_powers_of_two_minus_one():
    # log2(1+v) for v in {1,3,7} == {1,2,3}
    assert net_packets.log2_scale([1, 3, 7]) == [1.0, 2.0, 3.0]


def test_log2_scale_compresses_large_values():
    out = net_packets.log2_scale([0, 1023])
    assert out[0] == 0.0
    assert out[1] == 10.0  # log2(1024)


# ---- set_target_pids re-baselines to avoid a spike on set change --------

def test_set_target_pids_change_rebaselines_to_avoid_spike():
    mon = net_packets.PacketRateMonitor()
    mon.set_target_pids({1})
    mon._consume("t,proc.1,100,0,\n")  # baseline for pid 1 (rate 0)
    mon._consume("t,proc.1,150,0,\n")  # delta 50
    assert mon.rates() == [0, 50]
    # Set now includes pid 2 with a huge cumulative counter. A stale prev_total
    # would make the next delta ~100k; re-baselining yields 0 instead.
    mon.set_target_pids({1, 2})
    mon._consume("t,proc.1,160,0,\nt,proc.2,99999,0,\n")
    assert mon.rates() == [0, 50, 0]


def test_set_target_pids_same_set_keeps_baseline():
    mon = net_packets.PacketRateMonitor()
    mon.set_target_pids({1})
    mon._consume("t,proc.1,100,0,\n")  # baseline
    mon._consume("t,proc.1,150,0,\n")  # delta 50
    mon.set_target_pids({1})  # unchanged set -> no re-baseline
    mon._consume("t,proc.1,170,0,\n")  # delta 20
    assert mon.rates() == [0, 50, 20]


# ---- PacketRateMonitor --------------------------------------------------

class _FakeStream:
    """Canned nettop stream: a finite list of lines + a close flag."""

    def __init__(self, lines):
        self.lines = iter(lines)
        self.closed = False

    def close(self):
        self.closed = True


def _sample(pid, pin, pout):
    return f"12:00:00.0,proc.{pid},{pin},{pout},\n"


def _run_monitor(lines, pids):
    """Start a monitor on a finite fake stream and wait for the reader to drain."""
    stream = _FakeStream(lines)
    mon = net_packets.PacketRateMonitor(window=30, sampler=lambda: stream)
    mon.set_target_pids(pids)
    mon.start()
    mon._thread.join(timeout=2.0)  # finite stream -> thread exits on drain
    return mon


def test_monitor_computes_per_second_deltas_for_target_pid():
    # Three samples for pid 42: cumulative 100 -> 250 -> 400.
    # First sample = baseline (rate 0), then deltas 150, 150.
    lines = [
        "time,,packets_in,packets_out,\n", _sample(42, 100, 0),
        "time,,packets_in,packets_out,\n", _sample(42, 250, 0),
        "time,,packets_in,packets_out,\n", _sample(42, 400, 0),
    ]
    mon = _run_monitor(lines, {42})
    assert mon.rates() == [0, 150, 150]
    assert mon.current() == 150


def test_monitor_sums_in_and_out_and_ignores_other_pids():
    lines = [
        "time,,packets_in,packets_out,\n", _sample(1, 10, 5), _sample(2, 999, 999),
        "time,,packets_in,packets_out,\n", _sample(1, 40, 5), _sample(2, 999, 999),
    ]
    mon = _run_monitor(lines, {1})  # pid 2 excluded
    # baseline 15, then cur 45 -> delta 30
    assert mon.rates() == [0, 30]


def test_monitor_empty_before_start():
    mon = net_packets.PacketRateMonitor()
    assert mon.rates() == []
    assert mon.current() == 0


def test_monitor_stop_closes_stream():
    stream = _FakeStream([])
    mon = net_packets.PacketRateMonitor(sampler=lambda: stream)
    mon.start()
    mon._thread.join(timeout=2.0)
    mon.stop()
    assert stream.closed is True


def test_monitor_records_last_sample_time():
    mon = net_packets.PacketRateMonitor()
    assert mon.last_sample_at() is None  # nothing consumed yet
    driven = _run_monitor(
        ["time,,packets_in,packets_out,\n", _sample(42, 100, 0)], {42}
    )
    assert isinstance(driven.last_sample_at(), float)  # stamped on consume


def test_monitor_exposes_interval_and_window_defaults():
    mon = net_packets.PacketRateMonitor()
    assert mon.interval == 1.0
    assert mon.window == 30


def test_monitor_start_is_idempotent():
    calls = []

    def sampler():
        calls.append(1)
        return _FakeStream([])

    mon = net_packets.PacketRateMonitor(sampler=sampler)
    mon.start()
    mon.start()
    mon._thread.join(timeout=2.0)
    assert len(calls) == 1
