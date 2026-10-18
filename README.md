# haulcount

**Quarry haul-cycle and tonnage tracker for plain-text shift logs.**

haulcount reads one readable `.haul` file per site and turns it into tonnes per shift,
load-to-load cycle times, idle gaps, cost per tonne, end-of-shift forecasts and
weighbridge reconciliation. It runs offline on the Python standard library, and all
tonnage and money maths is exact (`Decimal`), so every total is one you can check by hand.

## Features

- **Plain-text shift log:** trucks, crew shifts and loads in one readable file, with `#` comments.
- **Line-numbered lint:** syntax errors, unknown trucks, overloads, loads outside the shift, and loads out of time order, each reported as `file:line`.
- **Strict lint for CI:** `lint --strict` turns warnings into failures.
- **Shift summaries:** loads, tonnes, tonnes per hour and per-truck breakdown for each shift.
- **Target tracking:** percent of target, shortfall in tonnes, and `summary --fail-below-target` as a CI gate.
- **JSON output:** `summary --json` prints every number as an exact string, not a float.
- **Cycle times:** min, mean and max load-to-load time per truck.
- **Idle-gap finder:** flags any wait longer than a set number of minutes (default 30).
- **Cost per tonne:** crew cost plus the cost of each truck that hauled that day, rounded half-up to the cent.
- **Forecast:** pace, projected end-of-shift tonnes, and whether the target is on track at a given time (`--now`), with no clock dependence.
- **Weighbridge reconciliation:** matches a ticket CSV to logged loads by truck, day, time and tonnage tolerance, and lists the unmatched on each side.
- **CSV export:** every load with running daily totals, ready for a spreadsheet.
- **Stable exit codes:** `0` clean, `1` errors or failed checks, `2` file or usage problems.

## Quick start

The shift log format is short:

```text
# truck <id> capacity <t> rate <cost per hour>
truck T1 capacity 20 rate 85.00
truck T2 capacity 15 rate 70.00

# crew <date> <start> <end> rate <crew cost per hour> [target <tonnes>]
crew 2026-10-12 06:00 14:00 rate 42.50 target 200
load 2026-10-12 06:14 T1 19.50 limestone
load 2026-10-12 06:31 T2 14.25 limestone
```

The full example in [`examples/north-pit.haul`](examples/north-pit.haul) has two shifts and 20 loads.
Run these from the project root:

```sh
python3 haulcount.py lint examples/north-pit.haul
python3 haulcount.py summary examples/north-pit.haul
python3 haulcount.py cycles examples/north-pit.haul
python3 haulcount.py cost examples/north-pit.haul
python3 haulcount.py forecast examples/north-pit.haul --date 2026-10-12 --now 10:00
python3 haulcount.py reconcile examples/north-pit.haul examples/weighbridge.csv
python3 haulcount.py csv examples/north-pit.haul > loads.csv
```

`reconcile` exits with `1` on the example on purpose. The last ticket on the sheet has no
matching load, and the second shift has no tickets, so those loads show as unmatched.

## How to run

haulcount needs Python 3.10 or newer and nothing else. There are no servers, accounts, API
keys or network calls. It is a single file, `haulcount.py`.

| Command | What it does |
| --- | --- |
| `lint FILE [--strict]` | Check the log and report `file:line` errors and warnings. |
| `summary FILE [--date D] [--json] [--fail-below-target]` | Tonnes, loads, t/h and target progress per shift. |
| `cycles FILE [--idle MIN]` | Load-to-load cycle stats per truck and idle gaps. |
| `cost FILE [--date D]` | Crew and truck cost, total and per tonne, per shift. |
| `forecast FILE --date D --now HH:MM` | Pace, projected tonnes and target status at a given time. |
| `reconcile FILE TICKETS.csv [--minutes N] [--tonnes T]` | Match loads to weighbridge tickets. |
| `csv FILE` | Export loads with running daily totals to stdout. |

Ticket files need a header row with `date,time,truck,tonnes`.

The rules for the log:

- `truck` needs `capacity` and `rate`; each truck id must be unique.
- `crew` needs a start and end time, with the end after the start. Shifts can't cross midnight.
- `load` needs a date, a time, a truck id and a positive tonnage. The material name is optional.
- Every load needs a crew line for its date, must fall inside that shift, and can't exceed its truck's capacity.

## How to test

The tests use `pytest`:

```sh
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install pytest
python3 -m pytest -q
```

The tests in `tests/test_haulcount.py` cover parsing, line-numbered errors, capacity and
shift-window checks, exact cost rounding, forecast status, reconciliation matching, CSV
export, and exit codes. `tests/conftest.py` adds the project root to the import path, so
pytest runs from any directory.

## Notes on the maths

- Cycle time is the gap between two consecutive loads by the same truck on the same shift.
- Cost per tonne charges the crew for the whole shift, and each truck that hauled that day
  for the whole shift at its hourly rate. Trucks that sit idle all day are not charged.
- Forecast pace is tonnes so far divided by hours elapsed since the shift started. The
  projection is pace multiplied by the full shift length.
- Reconciliation pairs each ticket with the closest unused load of the same truck and day
  that is within both tolerances. Tickets are processed in file order.
