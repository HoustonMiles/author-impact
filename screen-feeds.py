"""
RSS-first feasibility screen for the author-attribution study.

Substack (and most independent blogs) expose an RSS feed that hands you author and
publication timestamp as STRUCTURED fields. No LLM, no browser, no selector discovery,
no failure modes. Use this wherever a feed exists; save Yosoi for sites without one.

Usage:
    python screen_feeds.py feeds.txt

feeds.txt: one feed URL per line. For any Substack, append /feed to the publication
root (e.g. https://thebearcave.substack.com/feed). Blank lines, #-comments, and inline
"# x3 Publication Name" annotations are all stripped.

Writes to out/: domain_report.csv, bylines.csv, author_candidates.csv, feeds_resolved.txt

Normalisation, ticker detection, thresholds and scoring live in shared.py. This file
holds only what is specific to reading RSS.

KNOWN LIMIT: most feeds return only the ~20 most recent posts. That is plenty for
screening (does this source have named authors and real timestamps?) but NOT enough
history for a lead-rate study. Use vc_post_times.py for backfill.
"""

from __future__ import annotations

import calendar
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

from shared import (MIN_ITEMS, MIN_AUTHOR_RATE, MIN_DATE_RATE, OUT_DIR,
                    domain_of, dump_csv, normalize_byline, score_domain, tickers_in)

GO_DOMAIN_COUNT = 40

# Feed discovery. Three real failure modes from a live run, all recoverable:
#   * DNS  - apex vs www differ; www.netinterest.co resolves, netinterest.co does not
#   * HTML - /feed is the wrong path on some engines; try /rss, /feed.xml, ...
#   * XML  - feedparser fetching by URL sends no User-Agent and mishandles gzip;
#            fetch the bytes first, then parse those
FEED_PATHS = ('/feed', '/rss', '/feed.xml', '/index.xml', '/atom.xml', '/rss.xml')
UA = 'Mozilla/5.0 (compatible; CascadingLabs-research/0.1; +https://cascadinglabs.com)'
FETCH_TIMEOUT = 20

# Politeness delay between feeds. Without it, 208 back-to-back requests read as an
# attack: running the full screen twice in ten minutes made benzinga.com,
# theinformation.com and artofmanliness.com start returning 404/403 when they had
# served fine minutes earlier. Screen from out/feeds_resolved.txt on repeat runs to
# avoid re-hitting hosts that are already known dead.
REQUEST_DELAY = 1.0


def feed_variants(url: str) -> list[str]:
    """Ordered candidate URLs: host variants first, then alternate feed paths."""
    p = urlparse(url)
    host = p.netloc
    alt_host = host[4:] if host.startswith('www.') else 'www.' + host
    path = p.path.rstrip('/') or '/feed'
    out = [f'{p.scheme}://{host}{path}', f'{p.scheme}://{alt_host}{path}']
    for extra in FEED_PATHS:
        if extra != path:
            out.append(f'{p.scheme}://{host}{extra}')
    seen, ordered = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered[:6]


def fetch_feed(url: str, session: requests.Session):
    """Return (parsed_feed, working_url, error). Tries variants until one parses."""
    last = 'no candidates'
    for cand in feed_variants(url):
        try:
            resp = session.get(cand, timeout=FETCH_TIMEOUT, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            last = f'{type(exc).__name__}: {exc}'
            continue
        if resp.status_code != 200:
            last = f'HTTP {resp.status_code}'
            continue
        try:
            parsed = feedparser.parse(resp.content)   # bytes, not URL
        except Exception as exc:  # noqa: BLE001
            # feedparser itself raises on some malformed feeds (e.g. int(None) on a
            # <width> element with no value). One bad feed must not kill the run.
            last = f'parser crash: {type(exc).__name__}: {exc}'
            continue
        if parsed.entries:
            return parsed, cand, ''
        last = 'parsed but zero entries' if not parsed.bozo else str(
            parsed.get('bozo_exception', 'malformed')
        )
    return None, '', last


def entry_author(entry) -> str | None:
    """Feeds put the author in several places depending on generator."""
    for key in ('author', 'dc_creator', 'creator'):
        val = entry.get(key)
        if val:
            return str(val)
    detail = entry.get('author_detail') or {}
    return detail.get('name') or None


_TAG_RE = re.compile(r'<[^>]+>')


def entry_body(entry) -> str:
    """Post text from the feed. Substack ships full or partial content in RSS, so
    tickers named in the body are recoverable without fetching the article page.
    Headlines alone badly understate ticker specificity."""
    parts = []
    for c in (entry.get('content') or []):
        val = c.get('value') if isinstance(c, dict) else None
        if val:
            parts.append(val)
    for key in ('summary', 'subtitle', 'description'):
        val = entry.get(key)
        if val:
            parts.append(str(val))
    text = ' '.join(parts)
    return re.sub(r'\s+', ' ', _TAG_RE.sub(' ', text)).strip()


def entry_timestamp(entry) -> str | None:
    """Return an ISO-8601 UTC string, or None. struct_time from feedparser is UTC."""
    for key in ('published_parsed', 'updated_parsed'):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(
                calendar.timegm(st), tz=timezone.utc).isoformat()
    return None


def collect(urls: list[str]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    by_domain: dict[str, list[dict]] = defaultdict(list)
    errors: dict[str, str] = {}
    working: dict[str, str] = {}

    session = requests.Session()
    session.headers['User-Agent'] = UA

    for url in urls:
        dom = domain_of(url)
        feed, found_url, err = fetch_feed(url, session)

        if feed is None:
            errors[dom] = err
            print(f'  FAIL  {dom}: {err}')
            by_domain[dom] = []
            time.sleep(REQUEST_DELAY)
            continue

        working[dom] = found_url

        for e in feed.entries:
            by_domain[dom].append({
                'headline': e.get('title'),
                'author': entry_author(e),
                'published_at': entry_timestamp(e),
                'article_url': e.get('link'),
                'body': entry_body(e),
            })

        note = ' (partial parse)' if feed.bozo else ''
        extra = '' if found_url == url else f'  [via {found_url}]'
        print(f'  ok    {dom}: {len(feed.entries)} entries{note}{extra}')
        time.sleep(REQUEST_DELAY)

    # Record the URLs that actually worked, so the next run can skip variant retries.
    if working:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / 'feeds_resolved.txt').write_text(
            '\n'.join(sorted(working.values())) + '\n', encoding='utf-8'
        )

    return by_domain, errors


def write_reports(
    by_domain: dict[str, list[dict]],
    errors: dict[str, str],
) -> tuple[int, int]:
    domain_rows, byline_rows = [], []
    name_to_domains: dict[str, set[str]] = defaultdict(set)

    for dom, items in sorted(by_domain.items()):
        for i in items:
            raw = i.get('author')
            norm = normalize_byline(raw, dom)
            tks = tickers_in(i.get('headline')) | tickers_in(i.get('body'))
            byline_rows.append({
                'domain': dom,
                'raw_byline': raw or '',
                'normalized': norm,
                'published_at': i.get('published_at') or '',
                'headline': (i.get('headline') or '')[:120],
                'tickers': ' '.join(sorted(tks)),
                'body_chars': len(i.get('body') or ''),
            })
            if norm:
                name_to_domains[norm].add(dom)

        # Scoring and thresholds live in shared.py so every consumer agrees.
        row = score_domain(items, dom)
        row['error'] = errors.get(dom, '')
        domain_rows.append(row)

    candidate_rows = [
        {'normalized_name': name, 'n_domains': len(doms),
         'domains': '; '.join(sorted(doms))}
        for name, doms in sorted(name_to_domains.items(), key=lambda kv: -len(kv[1]))
        if len(doms) >= 2
    ]

    dump_csv('domain_report.csv', domain_rows)
    dump_csv('bylines.csv', byline_rows)
    dump_csv('author_candidates.csv', candidate_rows)
    return sum(1 for r in domain_rows if r['passes']), len(candidate_rows)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # Strip INLINE comments too - harvest_links.py annotates each line with
    # "    # x3 Publication Name", and '#' is a URL fragment delimiter, so leaving
    # it attached silently corrupts the path rather than erroring cleanly.
    urls = [
        u for u in (
            line.split('#', 1)[0].strip()
            for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
        ) if u
    ]
    print(f'Screening {len(urls)} feeds...\n')

    by_domain, errors = collect(urls)
    print()
    n_pass, n_candidates = write_reports(by_domain, errors)

    print(f"""
--- SUMMARY ---
feeds attempted   : {len(by_domain)}
feeds passing     : {n_pass}   (>={MIN_ITEMS} items, >={MIN_AUTHOR_RATE:.0%} author, \
>={MIN_DATE_RATE:.0%} date)
cross-domain names: {n_candidates}

Go/no-go: you wanted ~{GO_DOMAIN_COUNT} passing sources. You have {n_pass}.
Remember feeds usually cap at ~20 recent posts — this screens sources, not the corpus.
""")


if __name__ == '__main__':
    main()
