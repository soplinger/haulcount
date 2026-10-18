import csv
import datetime as dt
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

import haulcount as hc

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_LOG = str(EXAMPLES / "north-pit.haul")
EXAMPLE_TICKETS = str(EXAMPLES / "weighbridge.csv")
DAY = dt.date(2026, 10, 12)

BASE = """truck T1 capacity 20 rate 85.00
truck T2 capacity 15 rate 70.00
crew 2026-10-12 06:00 14:00 rate 42.50 target 200
"""


def write(tmp_path, text, name="shift.haul"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def parsed(text):
    haul, issues = hc.parse_text(text)
    return haul, issues + hc.check(haul)


def test_parse_time_accepts_hhmm_and_rejects_bad_values():
    assert hc.parse_time("06:30") == 390
    for bad in ("6:30", "24:00", "12:60", "1230"):
        with pytest.raises(ValueError):
            hc.parse_time(bad)


def test_parse_decimal_rejects_nan_infinity_and_junk():
    assert hc.parse_decimal("19.50", "tonnes") == Decimal("19.50")
    for bad in ("nan", "Infinity", "abc", ""):
        with pytest.raises(ValueError):
            hc.parse_decimal(bad, "tonnes")


def test_parse_date_rejects_impossible_dates():
    assert hc.parse_date("2026-10-12") == DAY
    with pytest.raises(ValueError):
        hc.parse_date("2026-02-30")


def test_unknown_directive_reports_its_line():
    _, issues = hc.parse_text("truck T1 capacity 20 rate 1\nbogus x\n")
    assert [(i.line, i.level) for i in issues] == [(2, "error")]
    assert "unknown directive" in issues[0].message


def test_duplicate_truck_is_a_syntax_error_on_second_line():
    _, issues = hc.parse_text("truck T1 capacity 20 rate 1\ntruck T1 capacity 9 rate 1\n")
    assert issues[0].line == 2
    assert "already defined on line 1" in issues[0].message


def test_zero_tonnes_is_rejected_at_parse_time():
    _, issues = hc.parse_text(BASE + "load 2026-10-12 06:30 T1 0\n")
    assert issues[0].line == 4
    assert "tonnes must be positive" in issues[0].message


def test_check_flags_unknown_truck_and_overload_but_allows_exact_capacity():
    text = BASE + ("load 2026-10-12 06:30 T9 5\n"
                   "load 2026-10-12 07:00 T2 15.5\n"
                   "load 2026-10-12 07:30 T1 20\n")
    _, issues = parsed(text)
    messages = {(i.line, i.message) for i in issues}
    assert (4, "unknown truck 'T9'") in messages
    assert any(line == 5 and "exceeds T2 capacity" in msg for line, msg in messages)
    assert not any(line == 6 for line, _ in messages)


def test_load_outside_shift_window_is_an_error():
    _, issues = parsed(BASE + "load 2026-10-12 14:30 T1 5\n")
    assert issues[0].level == "error"
    assert "outside the shift 06:00-14:00" in issues[0].message


def test_out_of_order_load_is_a_warning_and_strict_mode_fails(tmp_path):
    path = write(tmp_path, BASE + "load 2026-10-12 07:00 T1 5\nload 2026-10-12 06:30 T1 5\n")
    _, issues = parsed(Path(path).read_text(encoding="utf-8"))
    assert [(i.line, i.level) for i in issues] == [(5, "warning")]
    assert hc.main(["lint", path]) == 0
    assert hc.main(["lint", "--strict", path]) == 1


def test_example_log_lints_clean(capsys):
    assert hc.main(["lint", EXAMPLE_LOG]) == 0
    assert "OK (20 loads, 3 trucks, 2 shifts)" in capsys.readouterr().out


def test_summary_totals_match_hand_sums():
    haul, _ = hc.load_file(EXAMPLE_LOG)
    day1 = hc.day_summary(haul, DAY)
    assert day1["loads"] == 13
    assert day1["tonnes"] == Decimal("226.25")
    assert day1["target_pct"] == Decimal("113.13")
    assert day1["short_by"] is None
    assert day1["trucks"]["T1"] == {"loads": 5, "tonnes": Decimal("97.50")}
    day2 = hc.day_summary(haul, dt.date(2026, 10, 13))
    assert day2["tonnes"] == Decimal("121.50")
    assert day2["tonnes_per_hour"] == Decimal("20.25")
    assert day2["short_by"] == Decimal("28.50")


def test_summary_cli_json_keeps_numbers_as_exact_strings(capsys):
    assert hc.main(["summary", "--json", EXAMPLE_LOG]) == 0
    days = json.loads(capsys.readouterr().out)
    assert days[1]["date"] == "2026-10-13"
    assert days[1]["tonnes"] == "121.50"
    assert days[1]["target_pct"] == "81.00"


def test_fail_below_target_exit_code(capsys):
    assert hc.main(["summary", "--fail-below-target", EXAMPLE_LOG]) == 1
    assert hc.main(["summary", "--fail-below-target", "--date", "2026-10-12", EXAMPLE_LOG]) == 0


def test_cycle_gaps_for_example_truck_t1():
    haul, _ = hc.load_file(EXAMPLE_LOG)
    gaps = [end - start for _, truck, start, end in hc.cycle_gaps(haul) if truck == "T1"]
    assert gaps == [38, 57, 72, 64, 91]


def test_idle_gap_is_reported(tmp_path, capsys):
    text = ("truck T1 capacity 20 rate 1\n"
            "crew 2026-10-12 06:00 14:00 rate 1\n"
            "load 2026-10-12 06:10 T1 5\n"
            "load 2026-10-12 07:00 T1 5\n"
            "load 2026-10-12 07:20 T1 5\n")
    assert hc.main(["cycles", write(tmp_path, text)]) == 0
    out = capsys.readouterr().out
    assert "T1" in out and "35.00" in out
    assert "06:10 -> 07:00 (50 min)" in out
    assert "07:00 -> 07:20" not in out


def test_cost_is_exact_and_rounds_half_up():
    text = ("truck T1 capacity 20 rate 85.00\n"
            "truck T2 capacity 15 rate 70.00\n"
            "crew 2026-10-12 06:00 14:00 rate 42.50\n"
            "load 2026-10-12 06:30 T1 10\n"
            "load 2026-10-12 07:00 T1 10\n")
    haul, _ = parsed(text)
    row = hc.shift_costs(haul, [DAY])[0]
    assert row["crew_cost"] == Decimal("340.00")
    assert row["truck_cost"] == Decimal("680.00")
    assert row["total"] == Decimal("1020.00")
    assert row["per_tonne"] == Decimal("51.00")

    seven = text.replace("load 2026-10-12 06:30 T1 10\nload 2026-10-12 07:00 T1 10\n",
                         "load 2026-10-12 06:30 T1 7\n")
    haul, _ = parsed(seven)
    assert hc.shift_costs(haul, [DAY])[0]["per_tonne"] == Decimal("145.71")


BIG_TRUCK = "truck T1 capacity 100 rate 85.00\ncrew 2026-10-12 06:00 14:00 rate 42.50 target 200\n"


def test_forecast_on_track_and_behind():
    haul, _ = parsed(BIG_TRUCK + "load 2026-10-12 06:30 T1 40\nload 2026-10-12 07:30 T1 40\n")
    on_track = hc.forecast(haul, DAY, 8 * 60)
    assert on_track["pace"] == Decimal("40.00")
    assert on_track["projected"] == Decimal("320.00")
    assert on_track["status"].startswith("on track: 3.00 h needed")

    behind = hc.forecast(haul, DAY, 13 * 60)
    assert behind["status"] == "behind: 10.50 h needed at current pace, only 1.00 h left"


def test_forecast_before_start_and_after_target():
    haul, _ = parsed(BIG_TRUCK + "load 2026-10-12 06:30 T1 100\nload 2026-10-12 07:30 T1 100\n")
    assert hc.forecast(haul, DAY, 5 * 60)["status"] == "shift has not started"
    assert hc.forecast(haul, DAY, 9 * 60)["status"] == "target met"


def test_reconcile_matches_within_tolerance_and_reports_leftovers():
    loads = [hc.Load(DAY, 390, "T1", Decimal("19.50"), "x", 4)]
    tickets = [hc.Ticket(DAY, 391, "T1", Decimal("19.48"), 2)]
    matched, bad_tickets, bad_loads = hc.reconcile(loads, tickets, 5, Decimal("0.25"))
    assert len(matched) == 1 and not bad_tickets and not bad_loads
    matched, bad_tickets, bad_loads = hc.reconcile(loads, tickets, 5, Decimal("0.01"))
    assert not matched and len(bad_tickets) == 1 and len(bad_loads) == 1


def test_reconcile_cli_on_example(capsys):
    assert hc.main(["reconcile", EXAMPLE_LOG, EXAMPLE_TICKETS]) == 1
    out = capsys.readouterr().out
    assert "matched 13 of 14 tickets" in out
    assert "weighbridge.csv:15" in out


def test_csv_export_has_running_daily_totals(capsys):
    assert hc.main(["csv", EXAMPLE_LOG]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0] == ["date", "time", "truck", "tonnes", "day_total", "material"]
    assert rows[1] == ["2026-10-12", "06:14", "T1", "19.50", "19.50", "limestone"]
    assert rows[2] == ["2026-10-12", "06:31", "T2", "14.25", "33.75", "limestone"]


def test_summary_refuses_a_log_with_errors(tmp_path):
    path = write(tmp_path, BASE + "load 2026-10-12 06:30 T9 5\n")
    assert hc.main(["summary", path]) == 1


def test_missing_file_exits_with_code_2(tmp_path):
    assert hc.main(["lint", str(tmp_path / "nope.haul")]) == 2
