"""haulcount: quarry haul-cycle and tonnage tracker for plain-text shift logs.

Subcommands: lint, summary, cycles, cost, forecast, reconcile, csv.
Standard library only. All tonnage and money maths is exact (Decimal).
"""

import argparse
import csv
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
IDLE_MINUTES = 30
TICKET_FIELDS = ("date", "time", "truck", "tonnes")


@dataclass
class Truck:
    id: str
    capacity: Decimal
    rate: Decimal
    line: int


@dataclass
class Crew:
    date: dt.date
    start: int
    end: int
    rate: Decimal
    target: Decimal | None
    line: int

    @property
    def hours(self):
        return Decimal(self.end - self.start) / 60


@dataclass
class Load:
    date: dt.date
    minute: int
    truck: str
    tonnes: Decimal
    material: str
    line: int


@dataclass
class Ticket:
    date: dt.date
    minute: int
    truck: str
    tonnes: Decimal
    line: int


@dataclass
class Issue:
    line: int
    level: str
    message: str


@dataclass
class Haul:
    trucks: dict = field(default_factory=dict)
    crews: dict = field(default_factory=dict)
    loads: list = field(default_factory=list)


# ---------- parsing helpers ----------

def parse_time(text):
    hours, sep, minutes = text.partition(":")
    if sep != ":" or len(hours) != 2 or len(minutes) != 2 or not (hours + minutes).isdigit():
        raise ValueError(f"bad time {text!r}, expected HH:MM")
    hour, minute = int(hours), int(minutes)
    if hour > 23 or minute > 59:
        raise ValueError(f"time out of range {text!r}")
    return hour * 60 + minute


def parse_date(text):
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"bad date {text!r}, expected YYYY-MM-DD") from None


def parse_decimal(text, what):
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"bad {what} {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"bad {what} {text!r}")
    return value


def _hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _sum(values):
    return sum(values, Decimal(0))


def _options(tokens, allowed):
    """Turn 'key value key value' tokens into a dict, rejecting unknown or repeated keys."""
    if len(tokens) % 2:
        raise ValueError("options must come in 'key value' pairs")
    options = {}
    for key, value in zip(tokens[::2], tokens[1::2]):
        if key not in allowed:
            raise ValueError(f"unknown option {key!r}")
        if key in options:
            raise ValueError(f"option {key!r} given twice")
        options[key] = value
    return options


# ---------- shift-log parsing ----------

def _add_truck(haul, args, number):
    if not args:
        raise ValueError("truck needs an id")
    truck_id = args[0]
    opts = _options(args[1:], {"capacity", "rate"})
    if "capacity" not in opts or "rate" not in opts:
        raise ValueError("truck needs 'capacity' and 'rate'")
    if truck_id in haul.trucks:
        raise ValueError(f"truck {truck_id!r} already defined on line {haul.trucks[truck_id].line}")
    capacity = parse_decimal(opts["capacity"], "capacity")
    rate = parse_decimal(opts["rate"], "rate")
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    if rate < 0:
        raise ValueError("rate cannot be negative")
    haul.trucks[truck_id] = Truck(truck_id, capacity, rate, number)


def _add_crew(haul, args, number):
    if len(args) < 3:
        raise ValueError("crew needs DATE START END rate R [target T]")
    day = parse_date(args[0])
    start, end = parse_time(args[1]), parse_time(args[2])
    if end <= start:
        raise ValueError("shift must end after it starts")
    opts = _options(args[3:], {"rate", "target"})
    if "rate" not in opts:
        raise ValueError("crew needs 'rate'")
    if day in haul.crews:
        raise ValueError(f"crew for {day} already defined on line {haul.crews[day].line}")
    rate = parse_decimal(opts["rate"], "rate")
    if rate < 0:
        raise ValueError("rate cannot be negative")
    target = None
    if "target" in opts:
        target = parse_decimal(opts["target"], "target")
        if target <= 0:
            raise ValueError("target must be positive")
    haul.crews[day] = Crew(day, start, end, rate, target, number)


def _add_load(haul, args, number):
    if len(args) < 4:
        raise ValueError("load needs DATE HH:MM TRUCK TONNES [MATERIAL]")
    day = parse_date(args[0])
    minute = parse_time(args[1])
    tonnes = parse_decimal(args[3], "tonnes")
    if tonnes <= 0:
        raise ValueError("tonnes must be positive")
    material = " ".join(args[4:]) or "unspecified"
    haul.loads.append(Load(day, minute, args[2], tonnes, material, number))


DIRECTIVES = {"truck": _add_truck, "crew": _add_crew, "load": _add_load}


def parse_text(text):
    """Parse a shift log. Returns (Haul, syntax issues); bad lines are skipped."""
    haul = Haul()
    issues = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        handler = DIRECTIVES.get(tokens[0])
        try:
            if handler is None:
                raise ValueError(f"unknown directive {tokens[0]!r}")
            handler(haul, tokens[1:], number)
        except ValueError as exc:
            issues.append(Issue(number, "error", str(exc)))
    return haul, issues


def check(haul):
    """Cross-line checks: references, shift windows, capacity and ordering."""
    issues = []
    previous = {}
    for load in haul.loads:
        truck = haul.trucks.get(load.truck)
        crew = haul.crews.get(load.date)
        if truck is None:
            issues.append(Issue(load.line, "error", f"unknown truck {load.truck!r}"))
        elif load.tonnes > truck.capacity:
            issues.append(Issue(load.line, "error",
                                f"{load.tonnes} t exceeds {truck.id} capacity of {truck.capacity} t"))
        if crew is None:
            issues.append(Issue(load.line, "error", f"no crew line for {load.date}"))
        elif not crew.start <= load.minute <= crew.end:
            issues.append(Issue(load.line, "error",
                                f"load at {_hhmm(load.minute)} is outside the shift "
                                f"{_hhmm(crew.start)}-{_hhmm(crew.end)}"))
        last = previous.get(load.date)
        if last is not None and load.minute < last[0]:
            issues.append(Issue(load.line, "warning",
                                f"load at {_hhmm(load.minute)} is earlier than the load on line {last[1]}"))
        previous[load.date] = (load.minute, load.line)
    return issues


def load_file(path):
    with open(path, encoding="utf-8") as handle:
        haul, issues = parse_text(handle.read())
    issues += check(haul)
    return haul, sorted(issues, key=lambda issue: issue.line)


def _clean_haul(path):
    """Load a file for reporting. Returns None (after printing errors) if it has any errors."""
    haul, issues = load_file(path)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        for issue in errors:
            print(f"{path}:{issue.line}: error: {issue.message}", file=sys.stderr)
        print(f"haulcount: {len(errors)} error(s) in {path}; run 'haulcount lint {path}'", file=sys.stderr)
        return None
    return haul


def _loads_on(haul, day):
    return sorted((load for load in haul.loads if load.date == day), key=lambda load: load.minute)


# ---------- calculations ----------

def day_summary(haul, day):
    crew = haul.crews[day]
    day_loads = _loads_on(haul, day)
    tonnes = _sum(load.tonnes for load in day_loads)
    per_truck = {}
    for load in day_loads:
        count, total = per_truck.get(load.truck, (0, Decimal(0)))
        per_truck[load.truck] = (count + 1, total + load.tonnes)
    summary = {
        "date": day.isoformat(),
        "start": _hhmm(crew.start),
        "end": _hhmm(crew.end),
        "hours": money(crew.hours),
        "loads": len(day_loads),
        "tonnes": money(tonnes),
        "tonnes_per_hour": money(tonnes / crew.hours),
        "target": None,
        "target_pct": None,
        "short_by": None,
        "trucks": {truck: {"loads": count, "tonnes": money(total)}
                   for truck, (count, total) in sorted(per_truck.items())},
    }
    if crew.target is not None:
        summary["target"] = money(crew.target)
        summary["target_pct"] = money(tonnes * 100 / crew.target)
        if tonnes < crew.target:
            summary["short_by"] = money(crew.target - tonnes)
    return summary


def cycle_gaps(haul):
    """Load-to-load gaps per truck within each shift: (date, truck, from_minute, to_minute)."""
    gaps = []
    for day in sorted(haul.crews):
        by_truck = {}
        for load in _loads_on(haul, day):
            by_truck.setdefault(load.truck, []).append(load.minute)
        for truck, minutes in sorted(by_truck.items()):
            for start, end in zip(minutes, minutes[1:]):
                gaps.append((day, truck, start, end))
    return gaps


def shift_costs(haul, days):
    rows = []
    for day in days:
        crew = haul.crews[day]
        day_loads = _loads_on(haul, day)
        tonnes = _sum(load.tonnes for load in day_loads)
        used = sorted({load.truck for load in day_loads})
        crew_cost = money(crew.rate * crew.hours)
        truck_cost = money(_sum(haul.trucks[truck].rate for truck in used) * crew.hours)
        total = crew_cost + truck_cost
        rows.append({
            "date": day,
            "hours": money(crew.hours),
            "tonnes": money(tonnes),
            "crew_cost": crew_cost,
            "truck_cost": truck_cost,
            "total": total,
            "per_tonne": money(total / tonnes) if tonnes else None,
        })
    return rows


def forecast(haul, day, now):
    crew = haul.crews[day]
    done = [load for load in _loads_on(haul, day) if load.minute <= now]
    tonnes = _sum(load.tonnes for load in done)
    result = {
        "date": day.isoformat(),
        "now": _hhmm(now),
        "tonnes": money(tonnes),
        "pace": None,
        "projected": None,
        "target": None,
        "status": None,
    }
    elapsed = min(now, crew.end) - crew.start
    if elapsed <= 0:
        result["status"] = "shift has not started"
        return result
    pace = tonnes / (Decimal(elapsed) / 60)
    result["pace"] = money(pace)
    result["projected"] = money(pace * crew.hours)
    if crew.target is None:
        return result
    result["target"] = money(crew.target)
    remaining = crew.target - tonnes
    hours_left = Decimal(max(0, crew.end - now)) / 60
    if remaining <= 0:
        result["status"] = "target met"
    elif pace == 0:
        result["status"] = "no tonnes yet; target still open"
    else:
        hours_needed = remaining / pace
        if hours_needed <= hours_left:
            result["status"] = (f"on track: {money(hours_needed)} h needed at current pace, "
                                f"{money(hours_left)} h left")
        else:
            result["status"] = (f"behind: {money(hours_needed)} h needed at current pace, "
                                f"only {money(hours_left)} h left")
    return result


def reconcile(loads, tickets, minutes, tonnes_tolerance):
    """Greedy match: each ticket takes the closest unused load of the same truck and day."""
    used = set()
    matched, unmatched_tickets = [], []
    for ticket in tickets:
        best = None
        for index, load in enumerate(loads):
            if index in used or load.date != ticket.date or load.truck != ticket.truck:
                continue
            gap = abs(load.minute - ticket.minute)
            if gap <= minutes and abs(load.tonnes - ticket.tonnes) <= tonnes_tolerance:
                if best is None or gap < best[0]:
                    best = (gap, index)
        if best is None:
            unmatched_tickets.append(ticket)
        else:
            used.add(best[1])
            matched.append((ticket, loads[best[1]]))
    unmatched_loads = [load for index, load in enumerate(loads) if index not in used]
    return matched, unmatched_tickets, unmatched_loads


def read_tickets(path):
    tickets, issues = [], []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(TICKET_FIELDS) - set(reader.fieldnames or [])
        if missing:
            return [], [Issue(1, "error", f"missing columns: {', '.join(sorted(missing))}")]
        for row in reader:
            number = reader.line_num
            values = {name: (row.get(name) or "").strip() for name in TICKET_FIELDS}
            try:
                tickets.append(Ticket(parse_date(values["date"]), parse_time(values["time"]),
                                      values["truck"], parse_decimal(values["tonnes"], "tonnes"), number))
            except ValueError as exc:
                issues.append(Issue(number, "error", str(exc)))
    return tickets, issues


# ---------- output helpers ----------

def _plain(value):
    """Turn Decimals into strings so JSON output stays exact."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _print_day(day):
    print(f"{day['date']}  {day['start']}-{day['end']} ({day['hours']} h)  "
          f"{day['loads']} loads  {day['tonnes']} t  {day['tonnes_per_hour']} t/h")
    if day["target"] is not None:
        status = f"{day['target_pct']}% of {day['target']} t target"
        if day["short_by"] is not None:
            status += f", short by {day['short_by']} t"
        print(f"  {status}")
    for truck, figures in day["trucks"].items():
        print(f"  {truck:<8} {figures['loads']:>3} loads  {figures['tonnes']:>9} t")


# ---------- commands ----------

def cmd_lint(args):
    haul, issues = load_file(args.file)
    for issue in issues:
        print(f"{args.file}:{issue.line}: {issue.level}: {issue.message}")
    errors = sum(issue.level == "error" for issue in issues)
    warnings = len(issues) - errors
    if issues:
        print(f"{errors} error(s), {warnings} warning(s)")
    else:
        print(f"{args.file}: OK ({len(haul.loads)} loads, {len(haul.trucks)} trucks, "
              f"{len(haul.crews)} shifts)")
    return 1 if errors or (args.strict and warnings) else 0


def cmd_summary(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    if args.date and args.date not in haul.crews:
        print(f"haulcount: no shift recorded for {args.date}", file=sys.stderr)
        return 1
    days = [day_summary(haul, day) for day in ([args.date] if args.date else sorted(haul.crews))]
    if args.json:
        print(json.dumps(_plain(days), indent=2))
    elif not days:
        print("no shifts recorded")
    for day in days:
        if not args.json:
            _print_day(day)
    if args.fail_below_target and any(day["short_by"] is not None for day in days):
        return 1
    return 0


def cmd_cycles(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    gaps = cycle_gaps(haul)
    if not gaps:
        print("no truck made a repeat load, so there are no cycles to time")
        return 0
    per_truck = {}
    for _, truck, start, end in gaps:
        per_truck.setdefault(truck, []).append(end - start)
    print(f"{'truck':<8} {'cycles':>6} {'min':>5} {'mean':>7} {'max':>5}")
    for truck, times in sorted(per_truck.items()):
        mean = money(Decimal(sum(times)) / len(times))
        print(f"{truck:<8} {len(times):>6} {min(times):>5} {mean:>7} {max(times):>5}")
    idle = [gap for gap in gaps if gap[3] - gap[2] > args.idle]
    for day, truck, start, end in idle:
        print(f"idle: {day} {truck} {_hhmm(start)} -> {_hhmm(end)} ({end - start} min)")
    return 0


def cmd_cost(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    if args.date and args.date not in haul.crews:
        print(f"haulcount: no shift recorded for {args.date}", file=sys.stderr)
        return 1
    rows = shift_costs(haul, [args.date] if args.date else sorted(haul.crews))
    for row in rows:
        per_tonne = row["per_tonne"] if row["per_tonne"] is not None else "n/a"
        print(f"{row['date'].isoformat()}  {row['hours']} h  {row['tonnes']} t  "
              f"crew {row['crew_cost']}  trucks {row['truck_cost']}  "
              f"total {money(row['total'])}  per tonne {per_tonne}")
    if len(rows) > 1:
        tonnes = _sum(row["tonnes"] for row in rows)
        total = _sum(row["total"] for row in rows)
        per_tonne = money(total / tonnes) if tonnes else "n/a"
        print(f"TOTAL  {tonnes} t  total {money(total)}  per tonne {per_tonne}")
    return 0


def cmd_forecast(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    if args.date not in haul.crews:
        print(f"haulcount: no shift recorded for {args.date}", file=sys.stderr)
        return 1
    result = forecast(haul, args.date, args.now)
    print(f"{result['date']} at {result['now']}: {result['tonnes']} t so far")
    if result["status"] == "shift has not started":
        print(result["status"])
        return 0
    print(f"pace {result['pace']} t/h, projected {result['projected']} t by end of shift")
    if result["target"] is not None:
        print(f"target {result['target']} t: {result['status']}")
    return 0


def cmd_reconcile(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    tickets, issues = read_tickets(args.tickets)
    if issues:
        for issue in issues:
            print(f"{args.tickets}:{issue.line}: error: {issue.message}")
        return 1
    loads = sorted(haul.loads, key=lambda load: (load.date, load.minute))
    matched, bad_tickets, bad_loads = reconcile(loads, tickets, args.minutes, args.tonnes)
    variance = money(_sum(ticket.tonnes - load.tonnes for ticket, load in matched))
    print(f"matched {len(matched)} of {len(tickets)} tickets; "
          f"ticket minus load tonnage {variance:+} t")
    for ticket in bad_tickets:
        print(f"unmatched ticket: {args.tickets}:{ticket.line}  {ticket.date} "
              f"{_hhmm(ticket.minute)} {ticket.truck} {money(ticket.tonnes)} t")
    for load in bad_loads:
        print(f"unmatched load: {args.file}:{load.line}  {load.date} "
              f"{_hhmm(load.minute)} {load.truck} {money(load.tonnes)} t")
    return 1 if bad_tickets or bad_loads else 0


def cmd_csv(args):
    haul = _clean_haul(args.file)
    if haul is None:
        return 1
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["date", "time", "truck", "tonnes", "day_total", "material"])
    for day in sorted(haul.crews):
        running = Decimal(0)
        for load in _loads_on(haul, day):
            running += load.tonnes
            writer.writerow([day.isoformat(), _hhmm(load.minute), load.truck,
                             money(load.tonnes), money(running), load.material])
    return 0


# ---------- argument parsing ----------

def _arg(parse):
    def convert(text):
        try:
            return parse(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return convert


def build_parser():
    parser = argparse.ArgumentParser(
        prog="haulcount",
        description="Quarry haul-cycle and tonnage tracker for plain-text shift logs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lint", help="check a shift log line by line")
    p.add_argument("file")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("summary", help="tonnes, loads and target progress per shift")
    p.add_argument("file")
    p.add_argument("--date", type=_arg(parse_date), help="only this shift (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true", help="print JSON; numbers are exact strings")
    p.add_argument("--fail-below-target", action="store_true",
                   help="exit 1 if any shift with a target falls short")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("cycles", help="load-to-load cycle times and idle gaps")
    p.add_argument("file")
    p.add_argument("--idle", type=int, default=IDLE_MINUTES,
                   help=f"report gaps longer than this many minutes (default {IDLE_MINUTES})")
    p.set_defaults(func=cmd_cycles)

    p = sub.add_parser("cost", help="crew and truck cost per shift and per tonne")
    p.add_argument("file")
    p.add_argument("--date", type=_arg(parse_date), help="only this shift (YYYY-MM-DD)")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("forecast", help="end-of-shift projection and target pace")
    p.add_argument("file")
    p.add_argument("--date", type=_arg(parse_date), required=True, help="shift date (YYYY-MM-DD)")
    p.add_argument("--now", type=_arg(parse_time), required=True, help="current time (HH:MM)")
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("reconcile", help="match loads against a weighbridge ticket CSV")
    p.add_argument("file")
    p.add_argument("tickets", help="CSV with columns date,time,truck,tonnes")
    p.add_argument("--minutes", type=int, default=5, help="time tolerance in minutes (default 5)")
    p.add_argument("--tonnes", type=_arg(lambda text: parse_decimal(text, "tolerance")),
                   default=Decimal("0.25"), help="tonnage tolerance (default 0.25)")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("csv", help="export loads with running daily totals as CSV")
    p.add_argument("file")
    p.set_defaults(func=cmd_csv)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print(f"haulcount: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
