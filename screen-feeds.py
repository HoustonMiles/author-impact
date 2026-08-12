"""
RSS-first feasibility screen for the author-attribution study.

Substack (and most independent blogs) expose an RSS feed that hands you author and
publication timestamp as STRUCTURED fields. No LLM, no browser, no selector discovery,
no failure modes. Use this wherever a feed exists; save Yosoi for sites without one.

Usage:
    python screen_feeds.py feeds.txt

feeds.txt: one feed URL per line. For any Substack, append /feed to the publication
root (e.g. https://thebearcave.substack.com/feed). Blank lines and #-comments ignored.

Outputs the same three CSVs as screen_domains.py so the two are interchangeable:
  domain_report.csv, bylines.csv, author_candidates.csv

KNOWN LIMIT: most feeds return only the ~20 most recent posts. That is plenty for
screening (does this source have named authors and real timestamps?) but NOT enough
history for a lead-rate study. Solve backfill separately, after screening.
"""

from __future__ import annotations

import calendar
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

OUT_DIR = Path.cwd()  # write reports where you run it, not next to the script

MIN_ITEMS = 5
MIN_AUTHOR_RATE = 0.7
MIN_DATE_RATE = 0.7
MIN_RESOLVED_RATE = 0.5   # byline must resolve to a PERSON, not just be non-empty
MIN_TICKER_RATE = 0.15    # no tickers -> no lead rate -> useless, however good the writing
GO_DOMAIN_COUNT = 40


# --------------------------------------------------------------------------------------
# Byline normalization (identical rules to screen_domains.py)
# --------------------------------------------------------------------------------------

_PREFIX_RE = re.compile(r'^\s*(by|written by|posted by|author)\s*[:\-]?\s*', re.I)
_SEPARATORS = ('·', '|', '—', '–', ',')
_DATEISH_RE = re.compile(
    r'\b('
    r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
    r'|\d{4}-\d{2}-\d{2}'
    r'|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}'
    r'|\d+\s+(hour|minute|day|week|month)s?\s+ago'
    r')\b',
    re.I,
)
_NON_NAME_RE = re.compile(r'[^\w\s\.\'-]', re.UNICODE)

STAFF_TOKENS = {
    'staff', 'editor', 'editors', 'admin', 'newsroom', 'team', 'contributor',
    'guest', 'guest post', 'press release', 'reuters', 'associated press', 'ap',
}


def normalize_byline(raw: str | None, domain: str = '') -> str:
    """Reduce a printed byline to a comparable name key. Empty string means unusable.

    Two rules learned from real feed data:
      * Apostrophes are DELETED, not kept. Substack emits both "Doug O'Laughlin" and
        "Doug OLaughlin" for the same person; keeping the apostrophe splits one author
        into two identities on the same domain.
      * A byline matching the publication name ("The Bear Cave" on thebearcave.substack.com)
        is the publication posting as itself, not a person. Compared against the domain.
    """
    if not raw:
        return ''
    s = str(raw).strip()
    for sep in _SEPARATORS:
        if sep in s:
            s = s.split(sep)[0].strip()
    s = _PREFIX_RE.sub('', s)
    # Blogger emits "noreply@blogger.com (Real Name)" - keep the parenthesised name.
    m = re.match(r'^\S+@\S+\s*\((.+)\)\s*$', s)
    if m:
        s = m.group(1)
    s = re.sub(r'\S+@\S+', ' ', s)          # any other bare email in the byline
    s = _DATEISH_RE.sub('', s)
    s = s.replace("'", '').replace('\u2019', '')   # O'Laughlin == OLaughlin
    s = _NON_NAME_RE.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()

    if not s or s in STAFF_TOKENS:
        return ''
    if len(s.split()) < 2:
        return ''

    # Publication-as-author: compare compacted forms against the domain's first label.
    if domain:
        label = re.sub(r'[^a-z0-9]', '', domain.split('.')[0].lower())
        compact = re.sub(r'[^a-z0-9]', '', s)
        stripped = compact[3:] if compact.startswith('the') else compact
        for cand in {compact, stripped}:
            if cand and label and (cand in label or label in cand):
                return ''
    return s



# --------------------------------------------------------------------------------------
# Ticker detection
# --------------------------------------------------------------------------------------

# Explicit ticker forms seen in real headlines:
#   "Problems at StepStone ($STEP)"  -> $STEP
#   "Problems at DraftKings (DKNG)"  -> (DKNG)
#   "SpaceX (SPCX): Defying Gravity" -> (SPCX)
#   "Earnings: TSMC, NXPI, AEHR"     -> bare all-caps run
_CASHTAG_RE = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_TICKER_RE = re.compile(r'\(\s*\$?([A-Z]{2,5})\s*\)')
_BARE_RUN_RE = re.compile(r'\b([A-Z]{2,5})\b')

# All-caps words that are not tickers. Extend this as you see false positives —
# a bare-caps match is a weak signal and this list is what keeps it honest.
_NOT_TICKERS = {
    'AI', 'IPO', 'CEO', 'CFO', 'COO', 'IR', 'US', 'USA', 'UK', 'EU', 'GDP', 'CPI',
    'ETF', 'ESG', 'SEC', 'FDA', 'FED', 'NEW', 'THE', 'AND', 'FOR', 'NOTW', 'PART',
    'Q1', 'Q2', 'Q3', 'Q4', 'M&A', 'LLM', 'GPU', 'CPU', 'API', 'OS', 'PC',
    # economic/industry acronyms that appear parenthesised in body prose
    'MBA', 'PCE', 'SAAR', 'SPDJI', 'FCF', 'WSJ', 'CPS', 'LNG', 'MIRI', 'YOY',
    'EBITDA', 'ROIC', 'ROE', 'TAM', 'ARR', 'MRR', 'CAGR', 'NAV', 'REIT', 'SPAC',
    'FOMC', 'BLS', 'BEA', 'OECD', 'IMF', 'ECB', 'BOJ', 'PPI', 'ISM', 'PMI',
}


# Bare all-caps matching produced RIA, TV, EP, EOD, EUV, EV, II, FP as "tickers" on
# real data - almost all noise. Strong forms ($TICK / parenthesised) were clean.
# Leave this False unless you have a curated symbol universe to validate against.
ALLOW_BARE_CAPS = False

# Optional symbol universe. Body prose defines acronyms parenthetically - "the
# Mortgage Bankers Association (MBA)", "(PCE)", "(SAAR)" - so pattern matching alone
# cannot separate tickers from abbreviations. Drop a newline-delimited symbol list at
# TICKER_UNIVERSE_FILE and every match is validated against it.
#
#   curl -o symbols.txt https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt
#   cut -d'|' -f2 symbols.txt | tail -n +2 > tickers.txt
#
# Without the file, matching falls back to patterns + stoplist and WILL over-report.
TICKER_UNIVERSE_FILE = Path('tickers.txt')


def _load_universe() -> set[str] | None:
    if not TICKER_UNIVERSE_FILE.exists():
        return None
    syms = {
        ln.strip().upper()
        for ln in TICKER_UNIVERSE_FILE.read_text(encoding='utf-8').splitlines()
        if ln.strip() and ln.strip().isalpha()
    }
    return syms or None


_UNIVERSE = _load_universe()


def tickers_in(text: str | None) -> set[str]:
    """Extract probable ticker symbols from a headline.

    Strong signals ($TICK, parenthesised) are always accepted. Bare all-caps runs are
    accepted only if not in the stoplist — that branch WILL produce false positives,
    so treat the rate as an indicator for screening, not as extraction ground truth.
    """
    if not text:
        return set()
    found = set(_CASHTAG_RE.findall(text)) | set(_PAREN_TICKER_RE.findall(text))
    if ALLOW_BARE_CAPS:
        for m in _BARE_RUN_RE.findall(text):
            if m not in _NOT_TICKERS and len(m) >= 2:
                found.add(m)
    found = {t for t in found if t not in _NOT_TICKERS}
    if _UNIVERSE is not None:
        found = {t for t in found if t in _UNIVERSE}
    return found


# Feed discovery. Three real failure modes from a live run, all recoverable:
#   * DNS  - apex vs www differ; www.netinterest.co resolves, netinterest.co does not
#   * HTML - /feed is the wrong path on some engines; try /rss, /feed.xml, ...
#   * XML  - feedparser fetching by URL sends no User-Agent and mishandles gzip;
#            fetch bytes with requests first, then parse those
FEED_PATHS = ('/feed', '/rss', '/feed.xml', '/index.xml', '/atom.xml', '/rss.xml')
UA = 'Mozilla/5.0 (compatible; CascadingLabs-research/0.1; +https://cascadinglabs.com)'
FETCH_TIMEOUT = 20


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


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


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
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc).isoformat()
    return None


# --------------------------------------------------------------------------------------

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

    # Rewrite feeds.txt with the URLs that actually worked, so the next run is clean.
    if working:
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
        n = len(items)
        n_author = sum(1 for i in items if i.get('author'))
        n_date = sum(1 for i in items if i.get('published_at'))
        n_resolved = 0
        n_ticker = 0

        normalized_here = set()
        for i in items:
            raw = i.get('author')
            norm = normalize_byline(raw, dom)
            byline_rows.append({
                'domain': dom,
                'raw_byline': raw or '',
                'normalized': norm,
                'published_at': i.get('published_at') or '',
                'headline': (i.get('headline') or '')[:120],
            })
            tks = tickers_in(i.get('headline')) | tickers_in(i.get('body'))
            if tks:
                n_ticker += 1
            byline_rows[-1]['tickers'] = ' '.join(sorted(tks))
            byline_rows[-1]['body_chars'] = len(i.get('body') or '')
            if norm:
                n_resolved += 1
                normalized_here.add(norm)

        for norm in normalized_here:
            name_to_domains[norm].add(dom)

        author_rate = n_author / n if n else 0.0
        date_rate = n_date / n if n else 0.0
        domain_rows.append({
            'domain': dom,
            'items': n,
            'author_rate': round(author_rate, 3),
            'date_rate': round(date_rate, 3),
            'resolved_rate': round(n_resolved / n, 3) if n else 0.0,
            'ticker_rate': round(n_ticker / n, 3) if n else 0.0,
            'distinct_authors': len(normalized_here),
            'passes': (
                n >= MIN_ITEMS
                and author_rate >= MIN_AUTHOR_RATE
                and date_rate >= MIN_DATE_RATE
                and (n_resolved / n if n else 0) >= MIN_RESOLVED_RATE
                and (n_ticker / n if n else 0) >= MIN_TICKER_RATE
            ),
            'error': errors.get(dom, ''),
        })

    candidate_rows = [
        {'normalized_name': name, 'n_domains': len(doms), 'domains': '; '.join(sorted(doms))}
        for name, doms in sorted(name_to_domains.items(), key=lambda kv: -len(kv[1]))
        if len(doms) >= 2
    ]

    _dump('domain_report.csv', domain_rows)
    _dump('bylines.csv', byline_rows)
    _dump('author_candidates.csv', candidate_rows)
    return sum(1 for r in domain_rows if r['passes']), len(candidate_rows)


def _dump(name: str, rows: list[dict]) -> None:
    path = OUT_DIR / name
    if not rows:
        path.write_text('')
        print(f'  wrote {name} (empty)')
        return
    with path.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f'  wrote {name} ({len(rows)} rows)')


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
feeds passing     : {n_pass}   (>={MIN_ITEMS} items, >={MIN_AUTHOR_RATE:.0%} author, >={MIN_DATE_RATE:.0%} date)
cross-domain names: {n_candidates}

Go/no-go: you wanted ~{GO_DOMAIN_COUNT} passing sources. You have {n_pass}.
Remember feeds usually cap at ~20 recent posts — this screens sources, it is not the corpus.
""")


if __name__ == '__main__':
    main()
