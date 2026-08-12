"""
List the sources that passed screening, and check a watchlist against them.

Reads domain_report.csv (written by screen_feeds.py) and prints:

  1. every passing source, with its rates and author count
  2. a watchlist check - for each source you expected to qualify, whether it passed,
     and if not, WHICH criterion it failed and by how much

The second part is the useful one. "Not in the list" is not actionable; "failed on
ticker_rate 0.05 vs 0.15 threshold" tells you whether to adjust the screen, fix
detection, or accept that the source genuinely is not ticker-specific.

Usage:
    python check_sources.py                    # uses WATCHLIST below
    python check_sources.py watchlist.txt      # one domain substring per line

Stdlib only. Thresholds are read from screen_feeds.py so the two cannot drift.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPORT = Path('domain_report.csv')

# Sources this project specifically expects to qualify: forensic accounting, short
# research, and ticker-specific equity analysis. Substring match against the domain.
WATCHLIST = [
    'thebearcave',
    'readideabrunch',
    'nongaap',
    'dirtybubblemedia',
    'herbgreenberg',
    'petition11',
    'overlookedalpha',
    'microcapnewsletter',
    'flyoverstocks',
    'bestanchorstocks',
    'fabricatedknowledge',
    'citriniresearch',
    'yetanothervalueblog',
    'deepquarry',
    'the10thman',
    'tarotcapital',
    'basehitinvesting',
    'alluvial',
    'heavymoatinvestments',
    'asiancenturystocks',
    'compoundingquality',
    'thetranscript',
    'coldeye',
]

# Kept in sync with screen_feeds.py; imported below if that file is importable.
MIN_ITEMS = 5
MIN_AUTHOR_RATE = 0.7
MIN_DATE_RATE = 0.7
MIN_RESOLVED_RATE = 0.5
MIN_TICKER_RATE = 0.15


def load_thresholds() -> None:
    """Read thresholds from screen_feeds.py so this script can't drift from it."""
    global MIN_ITEMS, MIN_AUTHOR_RATE, MIN_DATE_RATE
    global MIN_RESOLVED_RATE, MIN_TICKER_RATE
    for name in ('screen_feeds.py', 'screen-feeds.py'):
        path = Path(name)
        if not path.exists():
            continue
        ns: dict = {}
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('MIN_') and '=' in line:
                key, _, val = line.partition('=')
                val = val.split('#')[0].strip()
                try:
                    ns[key.strip()] = float(val)
                except ValueError:
                    pass
        MIN_ITEMS = int(ns.get('MIN_ITEMS', MIN_ITEMS))
        MIN_AUTHOR_RATE = ns.get('MIN_AUTHOR_RATE', MIN_AUTHOR_RATE)
        MIN_DATE_RATE = ns.get('MIN_DATE_RATE', MIN_DATE_RATE)
        MIN_RESOLVED_RATE = ns.get('MIN_RESOLVED_RATE', MIN_RESOLVED_RATE)
        MIN_TICKER_RATE = ns.get('MIN_TICKER_RATE', MIN_TICKER_RATE)
        return


def num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def failures(row: dict) -> list[str]:
    """Which criteria this row missed, with the actual value against the threshold."""
    out = []
    checks = [
        ('items', num(row, 'items'), MIN_ITEMS, '{:.0f}'),
        ('author_rate', num(row, 'author_rate'), MIN_AUTHOR_RATE, '{:.2f}'),
        ('date_rate', num(row, 'date_rate'), MIN_DATE_RATE, '{:.2f}'),
        ('resolved_rate', num(row, 'resolved_rate'), MIN_RESOLVED_RATE, '{:.2f}'),
        ('ticker_rate', num(row, 'ticker_rate'), MIN_TICKER_RATE, '{:.2f}'),
    ]
    for label, value, threshold, fmt in checks:
        if value < threshold:
            out.append(f'{label} {fmt.format(value)} < {fmt.format(threshold)}')
    return out


def main() -> None:
    if not REPORT.exists():
        sys.exit(f'{REPORT} not found. Run screen_feeds.py first.')

    load_thresholds()

    watch = WATCHLIST
    if len(sys.argv) > 1:
        watch = [
            ln.strip().lower()
            for ln in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
            if ln.strip() and not ln.strip().startswith('#')
        ]

    rows = list(csv.DictReader(REPORT.open(encoding='utf-8')))
    passing = [r for r in rows if str(r.get('passes', '')).strip().lower() == 'true']
    passing.sort(key=lambda r: -num(r, 'ticker_rate'))

    print(f'\nTHRESHOLDS  items>={MIN_ITEMS}  author>={MIN_AUTHOR_RATE:.2f}  '
          f'date>={MIN_DATE_RATE:.2f}  resolved>={MIN_RESOLVED_RATE:.2f}  '
          f'ticker>={MIN_TICKER_RATE:.2f}')

    print(f'\nQUALIFYING SOURCES ({len(passing)} of {len(rows)})')
    print(f'  {"domain":42} {"posts":>5} {"resolv":>7} {"ticker":>7} {"authors":>8}')
    for r in passing:
        print(f'  {r["domain"]:42} {num(r, "items"):5.0f} '
              f'{num(r, "resolved_rate"):7.2f} {num(r, "ticker_rate"):7.2f} '
              f'{num(r, "distinct_authors"):8.0f}')

    # ---- watchlist ---------------------------------------------------------------
    by_domain = {r['domain'].lower(): r for r in rows}
    hits, misses, absent = [], [], []

    for want in watch:
        matched = [d for d in by_domain if want.lower() in d]
        if not matched:
            absent.append(want)
            continue
        for d in matched:
            row = by_domain[d]
            if str(row.get('passes', '')).strip().lower() == 'true':
                hits.append(d)
            else:
                misses.append((d, failures(row), row))

    print(f'\nWATCHLIST  ({len(watch)} entries)')
    print(f'\n  PASSED ({len(hits)}):')
    for d in sorted(hits):
        print(f'    + {d}')

    print(f'\n  SCREENED BUT FAILED ({len(misses)}):')
    for d, fails, row in sorted(misses):
        why = '; '.join(fails) or 'unknown'
        note = f'  [{row.get("error")}]' if row.get('error') else ''
        print(f'    - {d:42} {why}{note}')

    print(f'\n  NEVER COLLECTED ({len(absent)}):')
    for w in sorted(absent):
        print(f'    ? {w}   (not in feeds.txt, or the feed never parsed)')

    print(f'\nSUMMARY  {len(hits)}/{len(watch)} watchlist sources qualified.\n'
          '  "failed" entries are a screening decision - check whether the criterion\n'
          '  is right before changing it. "never collected" is a harvesting gap:\n'
          '  add the publication to feeds.txt directly and rescreen.\n')


if __name__ == '__main__':
    main()
