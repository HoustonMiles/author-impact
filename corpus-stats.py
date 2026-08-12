"""
Population statistics for the harvested corpus.

Reads bylines.csv (and domain_report.csv if present) and computes the two numbers
worth putting in a proposal, plus the supporting distributions:

  1. PERSON vs BRAND  - what share of financial newsletter posts carry a byline that
                        resolves to an identifiable person, rather than a publication
                        name ("Best Anchor Stocks") or a pseudonym ("Doomberg")?
  2. TICKER SPECIFICITY - what share mention an identifiable company, i.e. could
                        support a lead-lag test at all?

Usage:
    python corpus_stats.py                      # reads ./bylines.csv
    python corpus_stats.py path/to/bylines.csv

Writes stats_by_domain.csv and prints a summary. Stdlib only.
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

BYLINES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('bylines.csv')
DOMAIN_REPORT = Path('domain_report.csv')
OUT = Path('stats_by_domain.csv')

# A domain is "person-bylined" if a majority of its posts resolve to a person.
PERSON_DOMAIN_CUTOFF = 0.5


def pct(n: int, d: int) -> str:
    return f'{100.0 * n / d:5.1f}%' if d else '  n/a'


def quantiles(values: list[float]) -> str:
    if not values:
        return 'n/a'
    s = sorted(values)
    if len(s) < 4:
        return f'min {s[0]:.2f}  max {s[-1]:.2f}'
    q = statistics.quantiles(s, n=4)
    return (f'min {s[0]:.2f}  p25 {q[0]:.2f}  median {q[1]:.2f}  '
            f'p75 {q[2]:.2f}  max {s[-1]:.2f}')


def histogram(values: list[float], width: int = 40) -> list[str]:
    """Deciles, so bimodality is visible rather than hidden behind a mean."""
    buckets = [0] * 10
    for v in values:
        buckets[min(int(v * 10), 9)] += 1
    top = max(buckets) or 1
    out = []
    for i, count in enumerate(buckets):
        label = f'{i / 10:.1f}-{(i + 1) / 10:.1f}'
        bar = '#' * round(width * count / top)
        out.append(f'  {label}  {count:4d} |{bar}')
    return out


def main() -> None:
    if not BYLINES.exists():
        sys.exit(f'{BYLINES} not found. Run screen_feeds.py first.')

    rows = list(csv.DictReader(BYLINES.open(encoding='utf-8')))
    if not rows:
        sys.exit(f'{BYLINES} is empty.')

    has_tickers = 'tickers' in rows[0]

    passes: dict[str, bool] = {}
    if DOMAIN_REPORT.exists():
        for r in csv.DictReader(DOMAIN_REPORT.open(encoding='utf-8')):
            passes[r['domain']] = str(r.get('passes', '')).strip().lower() == 'true'

    per_domain: dict[str, dict] = defaultdict(
        lambda: {'n': 0, 'resolved': 0, 'ticker': 0, 'authors': set()}
    )
    author_posts: Counter = Counter()
    author_domains: dict[str, set[str]] = defaultdict(set)
    ticker_counts: Counter = Counter()

    n_posts = n_resolved = n_ticker = 0

    for r in rows:
        dom = r['domain']
        d = per_domain[dom]
        d['n'] += 1
        n_posts += 1

        norm = (r.get('normalized') or '').strip()
        if norm:
            d['resolved'] += 1
            d['authors'].add(norm)
            n_resolved += 1
            author_posts[norm] += 1
            author_domains[norm].add(dom)

        if has_tickers:
            tks = (r.get('tickers') or '').split()
            if tks:
                d['ticker'] += 1
                n_ticker += 1
                ticker_counts.update(tks)

    # ---- per-domain table -------------------------------------------------------
    table = []
    for dom, d in sorted(per_domain.items()):
        table.append({
            'domain': dom,
            'posts': d['n'],
            'resolved_rate': round(d['resolved'] / d['n'], 3),
            'ticker_rate': round(d['ticker'] / d['n'], 3) if has_tickers else '',
            'distinct_authors': len(d['authors']),
            'person_bylined': d['resolved'] / d['n'] >= PERSON_DOMAIN_CUTOFF,
            'passes_screen': passes.get(dom, ''),
        })
    with OUT.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)

    res_rates = [t['resolved_rate'] for t in table]
    tick_rates = [t['ticker_rate'] for t in table] if has_tickers else []
    n_person_domains = sum(1 for t in table if t['person_bylined'])
    multi = {a: ds for a, ds in author_domains.items() if len(ds) > 1}

    print(f"""
CORPUS
  posts                {n_posts}
  domains              {len(per_domain)}
  distinct authors     {len(author_posts)}

1. PERSON vs BRAND BYLINES
  posts resolving to a person      {n_resolved:5d} / {n_posts}  ({pct(n_resolved, n_posts)})
  posts under a brand or pseudonym {n_posts - n_resolved:5d} / {n_posts}  \
({pct(n_posts - n_resolved, n_posts)})

  domains majority-person-bylined  {n_person_domains:5d} / {len(table)}  \
({pct(n_person_domains, len(table))})
  per-domain resolved rate: {quantiles(res_rates)}

  distribution of per-domain resolved rate:""")
    for line in histogram(res_rates):
        print(line)

    if has_tickers:
        print(f"""
2. TICKER SPECIFICITY
  posts naming >=1 ticker          {n_ticker:5d} / {n_posts}  ({pct(n_ticker, n_posts)})
  per-domain ticker rate: {quantiles(tick_rates)}

  distribution of per-domain ticker rate:""")
        for line in histogram(tick_rates):
            print(line)
        top = ', '.join(f'{t} ({c})' for t, c in ticker_counts.most_common(15))
        print(f'\n  most-mentioned symbols: {top}')
    else:
        print('\n2. TICKER SPECIFICITY\n  no `tickers` column in bylines.csv — '
              'rerun screen_feeds.py with the ticker patch applied.')

    print(f"""
AUTHOR CONCENTRATION
  authors on >1 domain             {len(multi)}""")
    for a, ds in sorted(multi.items()):
        print(f'    {a:28} {"; ".join(sorted(ds))}')
    print('\n  most prolific authors:')
    for a, c in author_posts.most_common(10):
        print(f'    {a:28} {c:4d} posts')

    print(f'\n  wrote {OUT}\n')
    print('CAVEAT: feeds cap at ~20 recent posts, so per-domain counts reflect '
          'recency, not\noutput. Rates are comparable across domains; totals are not.\n')


if __name__ == '__main__':
    main()
