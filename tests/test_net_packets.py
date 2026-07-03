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


# ---- moving_average -----------------------------------------------------

def test_moving_average_empty_is_empty():
    assert net_packets.moving_average([], 2) == []


def test_moving_average_single_value():
    assert net_packets.moving_average([10], 2) == [10.0]


def test_moving_average_trailing_window():
    # i0: mean[10]=10; i1: mean[10,20]=15
    assert net_packets.moving_average([10, 20], 2) == [10.0, 15.0]


def test_moving_average_window_of_two_smooths_spike():
    assert net_packets.moving_average([0, 100, 100, 100], 2) == [0.0, 50.0, 100.0, 100.0]


def test_moving_average_window_one_is_identity():
    assert net_packets.moving_average([2, 4, 6], 1) == [2.0, 4.0, 6.0]


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
