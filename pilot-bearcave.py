"""
Pilot event study: does one author's short-research reports precede price moves?

Source: The Bear Cave (Edwin Dorsey). Chosen because it is close to a purpose-built
test case — ~150 reports each naming ONE company in the URL slug, a fixed publication
schedule (Thursdays ~10:30am ET), and documented claims of market impact.

    python pilot_bearcave.py                  # full run
    python pilot_bearcave.py --events-only    # build the event list, skip prices

Pipeline:
  1. Parse sitemap.xml for /p/problems-at-* style slugs and their <lastmod> dates
  2. Extract the ticker from the slug tail, validate against tickers.txt if present
  3. Download daily prices (yfinance) for each ticker and a market proxy
  4. Market-model event study: estimate alpha/beta on a clean pre-window, then
     compute abnormal returns and CARs around the publication date

Outputs events.csv, event_returns.csv, and a printed summary.

READ THIS BEFORE BELIEVING ANY NUMBER
  * <lastmod> is a MODIFICATION date. On this sitemap everything before ~2023 was
    overwritten by a site migration (issues #1-#40 all read 2023-01-23). Events before
    MIN_RELIABLE_DATE are excluded by default. Even after it, a later edit can shift a
    date — treat dates as approximate until verified against the article page.
  * Daily bars cannot separate "the report moved the stock" from "news moved both".
    A same-day negative CAR is CONSISTENT with impact, not evidence of it. The pre-event
    window is the diagnostic: if abnormal volume/returns appear BEFORE publication, the
    report is likely following the move. That is what Machus et al. (2022) found for
    Trump tweets using minute data.
  * Small N, no multiple-comparison correction, survivorship in the ticker universe,
    and delisted names silently dropped. This is a feasibility pilot, not a result.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SITEMAP = 'https://thebearcave.substack.com/sitemap.xml'
MARKET_PROXY = 'SPY'

# urllib's default UA ("Python-urllib/3.x") is 403'd by Substack. Send a real one.
UA = 'Mozilla/5.0 (compatible; CascadingLabs-research/0.1; +https://cascadinglabs.com)'

# Sitemap dates before this are migration artifacts, not publication dates.
MIN_RELIABLE_DATE = '2023-06-01'

ESTIMATION_START, ESTIMATION_END = -250, -31   # trading days rel. to event
EVENT_WINDOW = (-5, 5)
CAR_WINDOWS = [(-5, -1), (0, 0), (0, 1), (0, 5), (1, 5)]

# Slugs that name a company. "the-bear-cave-337" is a roundup, not a single-name report.
REPORT_SLUG = re.compile(r'^/p/((even-)?more-)?problems-at-|^/p/potential-at-')
TICKER_TAIL = re.compile(r'-([a-z]{1,5})(-\d+)?$')

NOT_TICKERS = {
    'inc', 'corp', 'group', 'ltd', 'plc', 'co', 'llc', 'the', 'and', 'part',
    'nyse', 'nasdaq', 'limited', 'holdings', 'technologies', 'companies',
}


def load_universe() -> set[str] | None:
    path = Path('tickers.txt')
    if not path.exists():
        return None
    syms = {
        ln.strip().upper()
        for ln in path.read_text(encoding='utf-8').splitlines()
        if ln.strip().isalpha()
    }
    return syms or None


def fetch_events(universe: set[str] | None) -> pd.DataFrame:
    """Parse the sitemap into (url, date, ticker) rows for single-company reports."""
    resp = requests.get(SITEMAP, headers={'User-Agent': UA}, timeout=60)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    rows = []
    for url in root.findall('s:url', ns):
        loc = url.findtext('s:loc', default='', namespaces=ns)
        lastmod = url.findtext('s:lastmod', default='', namespaces=ns)
        path = loc.split('substack.com', 1)[-1]
        if not REPORT_SLUG.search(path) or not lastmod:
            continue

        m = TICKER_TAIL.search(path)
        ticker = m.group(1).upper() if m else None
        if ticker and (ticker.lower() in NOT_TICKERS or len(ticker) < 2):
            ticker = None
        if ticker and universe is not None and ticker not in universe:
            ticker = None

        rows.append({
            'url': loc,
            'slug': path,
            'date': lastmod,
            'ticker': ticker,
            'reliable_date': lastmod >= MIN_RELIABLE_DATE,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values('date').reset_index(drop=True)


def market_model_event(
    prices: pd.DataFrame, market: pd.Series, ticker: str, event_date: pd.Timestamp
) -> dict | None:
    """Estimate alpha/beta on a pre-event window, return CARs around the event."""
    if ticker not in prices.columns:
        return None

    stock_ret = prices[ticker].pct_change()
    mkt_ret = market.pct_change()
    both = pd.concat([stock_ret, mkt_ret], axis=1, keys=['stock', 'mkt']).dropna()
    if both.empty:
        return None

    idx = both.index.searchsorted(event_date)
    if idx <= abs(ESTIMATION_START) or idx >= len(both) - EVENT_WINDOW[1]:
        return None   # not enough history or not enough post-event data

    est = both.iloc[idx + ESTIMATION_START: idx + ESTIMATION_END]
    if len(est) < 60:
        return None

    beta, alpha = np.polyfit(est['mkt'], est['stock'], 1)
    resid_sd = float((est['stock'] - (alpha + beta * est['mkt'])).std())
    if not np.isfinite(resid_sd) or resid_sd == 0:
        return None

    win = both.iloc[idx + EVENT_WINDOW[0]: idx + EVENT_WINDOW[1] + 1].copy()
    win['ar'] = win['stock'] - (alpha + beta * win['mkt'])
    win['rel'] = range(EVENT_WINDOW[0], EVENT_WINDOW[0] + len(win))

    out = {
        'ticker': ticker,
        'event_date': event_date.date().isoformat(),
        'beta': round(float(beta), 3),
        'resid_sd': round(resid_sd, 4),
        'n_estimation': len(est),
    }
    for lo, hi in CAR_WINDOWS:
        sel = win[(win['rel'] >= lo) & (win['rel'] <= hi)]
        car = float(sel['ar'].sum())
        out[f'car_{lo}_{hi}'] = round(car, 4)
        # standardised CAR: scale by sd * sqrt(days) so windows are comparable
        out[f'scar_{lo}_{hi}'] = round(car / (resid_sd * np.sqrt(max(len(sel), 1))), 3)
    return out


def summarise(res: pd.DataFrame) -> None:
    print(f'\nEVENT STUDY  (n = {len(res)})')
    print(f'  {"window":>10} {"mean CAR":>10} {"median":>9} {"% neg":>7} '
          f'{"mean SCAR":>10} {"t":>7}')
    for lo, hi in CAR_WINDOWS:
        col, scol = f'car_{lo}_{hi}', f'scar_{lo}_{hi}'
        vals, svals = res[col].dropna(), res[scol].dropna()
        if vals.empty:
            continue
        t = svals.mean() / (svals.std(ddof=1) / np.sqrt(len(svals))) if len(svals) > 1 else np.nan
        print(f'  {f"[{lo},{hi}]":>10} {vals.mean():9.2%} {vals.median():8.2%} '
              f'{(vals < 0).mean():6.0%} {svals.mean():10.3f} {t:7.2f}')

    print("""
  Read [-5,-1] FIRST. Material abnormal movement before publication means the report
  is at least partly following the news, not leading it. A large [0,0] with a flat
  [-5,-1] is the pattern consistent with impact. Neither proves causation on daily bars.
  t-statistics assume independent events; overlapping windows and clustered publication
  dates violate that, so treat them as descriptive.""")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--events-only', action='store_true')
    ap.add_argument('--include-unreliable', action='store_true',
                    help='keep pre-2023-06 dates (migration-corrupted lastmod)')
    args = ap.parse_args()

    universe = load_universe()
    print(f'ticker universe: {"loaded (" + str(len(universe)) + ")" if universe else "none — slug tails unvalidated"}')

    events = fetch_events(universe)
    if events.empty:
        sys.exit('No report URLs found in sitemap.')

    events.to_csv('events.csv', index=False)
    usable = events[events['ticker'].notna()]
    if not args.include_unreliable:
        usable = usable[usable['reliable_date']]

    print(f'\nreport URLs found      : {len(events)}')
    print(f'  with a ticker in slug: {events["ticker"].notna().sum()}')
    print(f'  after date filter    : {len(usable)}  (>= {MIN_RELIABLE_DATE})')
    print(f'  date range           : {usable["date"].min().date()} to {usable["date"].max().date()}')
    print(f'  distinct tickers     : {usable["ticker"].nunique()}')
    print('  wrote events.csv')

    if args.events_only:
        return

    import yfinance as yf

    tickers = sorted(usable['ticker'].unique())
    start = (usable['date'].min() - pd.Timedelta(days=420)).date()
    end = (usable['date'].max() + pd.Timedelta(days=30)).date()
    print(f'\ndownloading {len(tickers)} tickers + {MARKET_PROXY} ({start} to {end})...')

    data = yf.download(tickers + [MARKET_PROXY], start=start, end=end,
                       auto_adjust=True, progress=False)
    closes = data['Close'] if 'Close' in data else data
    if MARKET_PROXY not in closes.columns:
        sys.exit(f'Could not download {MARKET_PROXY}.')
    market = closes[MARKET_PROXY]

    results, skipped = [], 0
    for _, row in usable.iterrows():
        r = market_model_event(closes, market, row['ticker'], row['date'])
        if r is None:
            skipped += 1
            continue
        r['slug'] = row['slug']
        results.append(r)

    if not results:
        sys.exit('No events had usable price history.')

    res = pd.DataFrame(results)
    res.to_csv('event_returns.csv', index=False)
    print(f'  usable events: {len(res)}   skipped (no/short history): {skipped}')
    summarise(res)
    print('  wrote event_returns.csv\n')


if __name__ == '__main__':
    main()
