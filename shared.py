"""
Shared logic for the author-attribution pipeline.

This module exists because normalize_byline, tickers_in, the thresholds and the report
writer were duplicated verbatim across screen_feeds.py and screen_domains.py. Every fix
during development had to be applied twice, and twice is how two copies drift apart.

Nothing here fetches anything. Fetching is VoidCrawl's job; this is parsing, normalising
and scoring only.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------------------
# Screening thresholds. Set these BEFORE looking at results.
# --------------------------------------------------------------------------------------

MIN_ITEMS = 5             # too few posts to judge a source
MIN_AUTHOR_RATE = 0.7     # share of posts with a non-empty byline field
MIN_DATE_RATE = 0.7       # share with a parseable timestamp
MIN_RESOLVED_RATE = 0.5   # share whose byline resolves to a PERSON, not just non-empty
MIN_TICKER_RATE = 0.15    # no tickers -> no lead rate -> unusable, however good the writing

# --------------------------------------------------------------------------------------
# Byline normalisation
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
_EMAIL_WRAPPED_RE = re.compile(r'^\S+@\S+\s*\((.+)\)\s*$')

# Words marking a byline as a publication rather than a person.
_PUBLICATION_WORDS = {
    'capital', 'research', 'media', 'newsletter', 'letter', 'stocks', 'stock',
    'alpha', 'insight', 'insights', 'report', 'reports', 'journal', 'daily',
    'weekly', 'brief', 'briefing', 'review', 'quality', 'investing', 'investments',
    'analytics', 'analysis', 'partners', 'advisors', 'management', 'fund', 'funds',
}

STAFF_TOKENS = {
    'staff', 'editor', 'editors', 'admin', 'newsroom', 'team', 'contributor',
    'guest', 'guest post', 'press release', 'reuters', 'associated press', 'ap',
}


def normalize_byline(raw: str | None, domain: str = '') -> str:
    """Reduce a printed byline to a comparable name key. '' means unusable.

    Rules learned from real feed data, each from an observed failure:
      * Apostrophes are DELETED. Substack emits both "Doug O'Laughlin" and
        "Doug OLaughlin" for one person; keeping them splits one author into two.
      * Blogger emits "noreply@blogger.com (Real Name)" — keep the parenthesised name.
      * A byline matching the domain is usually the publication posting as itself, BUT
        eponymous sites (herbgreenberg.com) are named after the person, so only treat a
        domain match as a brand when the byline reads like a publication.
    """
    if not raw:
        return ''
    s = str(raw).strip()

    for sep in _SEPARATORS:
        if sep in s:
            s = s.split(sep)[0].strip()

    s = _PREFIX_RE.sub('', s)
    m = _EMAIL_WRAPPED_RE.match(s)
    if m:
        s = m.group(1)
    s = re.sub(r'\S+@\S+', ' ', s)
    s = _DATEISH_RE.sub('', s)
    s = s.replace("'", '').replace('\u2019', '')
    s = _NON_NAME_RE.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()

    if not s or s in STAFF_TOKENS or len(s.split()) < 2:
        return ''

    looks_like_publication = (
        len(s.split()) >= 3
        or s.startswith('the ')
        or any(w in s.split() for w in _PUBLICATION_WORDS)
    )
    if domain and looks_like_publication:
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

_CASHTAG_RE = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_TICKER_RE = re.compile(r'\(\s*\$?([A-Z]{2,5})\s*\)')
_BARE_RUN_RE = re.compile(r'\b([A-Z]{2,5})\b')

# Bare all-caps matching returned RIA, TV, EP, EOD, EUV, EV as "tickers" on real data.
# Leave False unless validating against a curated symbol universe.
ALLOW_BARE_CAPS = False

_NOT_TICKERS = {
    'AI', 'IPO', 'CEO', 'CFO', 'COO', 'IR', 'US', 'USA', 'UK', 'EU', 'GDP', 'CPI',
    'ETF', 'ESG', 'SEC', 'FDA', 'FED', 'NEW', 'THE', 'AND', 'FOR', 'NOTW', 'PART',
    'Q1', 'Q2', 'Q3', 'Q4', 'M&A', 'LLM', 'GPU', 'CPU', 'API', 'OS', 'PC',
    # economic/industry acronyms that appear parenthesised in body prose
    'MBA', 'PCE', 'SAAR', 'SPDJI', 'FCF', 'WSJ', 'CPS', 'LNG', 'MIRI', 'YOY',
    'EBITDA', 'ROIC', 'ROE', 'TAM', 'ARR', 'MRR', 'CAGR', 'NAV', 'REIT', 'SPAC',
    'FOMC', 'BLS', 'BEA', 'OECD', 'IMF', 'ECB', 'BOJ', 'PPI', 'ISM', 'PMI',
}

TICKER_UNIVERSE_FILE = Path('data/tickers.txt')


def load_universe() -> set[str] | None:
    """Listed symbols, if available. Without it, matching over-reports.

        curl -o symbols.txt https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt
        cut -d'|' -f2 symbols.txt | tail -n +2 > data/tickers.txt
    """
    if not TICKER_UNIVERSE_FILE.exists():
        return None
    syms = {
        ln.strip().upper()
        for ln in TICKER_UNIVERSE_FILE.read_text(encoding='utf-8').splitlines()
        if ln.strip().isalpha()
    }
    return syms or None


_UNIVERSE = load_universe()


def tickers_in(text: str | None) -> set[str]:
    """Probable ticker symbols. Strong forms only unless ALLOW_BARE_CAPS is set."""
    if not text:
        return set()
    found = set(_CASHTAG_RE.findall(text)) | set(_PAREN_TICKER_RE.findall(text))
    if ALLOW_BARE_CAPS:
        found |= {m for m in _BARE_RUN_RE.findall(text) if len(m) >= 2}
    found = {t for t in found if t not in _NOT_TICKERS}
    if _UNIVERSE is not None:
        found = {t for t in found if t in _UNIVERSE}
    return found


# --------------------------------------------------------------------------------------
# URLs and timestamps
# --------------------------------------------------------------------------------------

def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def iso(value: str | None) -> str | None:
    """Normalise to UTC ISO. Returns None for date-only values dressed as midnight."""
    if not value:
        return None
    v = str(value).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return None
    return dt.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

OUT_DIR = Path('out')


def dump_csv(name: str, rows: list[dict]) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / name
    if not rows:
        path.write_text('')
        print(f'  wrote {path} (empty)')
        return
    with path.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f'  wrote {path} ({len(rows)} rows)')


def score_domain(items: list[dict], domain: str) -> dict:
    """Per-domain screening metrics from a list of post dicts.

    Each item needs: headline, author, published_at. Optional: body.
    """
    n = len(items)
    if not n:
        return {'domain': domain, 'items': 0, 'author_rate': 0.0, 'date_rate': 0.0,
                'resolved_rate': 0.0, 'ticker_rate': 0.0, 'distinct_authors': 0,
                'passes': False}

    n_author = sum(1 for i in items if i.get('author'))
    n_date = sum(1 for i in items if i.get('published_at'))
    n_resolved = n_ticker = 0
    authors = set()

    for i in items:
        norm = normalize_byline(i.get('author'), domain)
        if norm:
            n_resolved += 1
            authors.add(norm)
        if tickers_in(i.get('headline')) | tickers_in(i.get('body')):
            n_ticker += 1

    rates = {
        'author_rate': n_author / n,
        'date_rate': n_date / n,
        'resolved_rate': n_resolved / n,
        'ticker_rate': n_ticker / n,
    }
    return {
        'domain': domain,
        'items': n,
        **{k: round(v, 3) for k, v in rates.items()},
        'distinct_authors': len(authors),
        'passes': (
            n >= MIN_ITEMS
            and rates['author_rate'] >= MIN_AUTHOR_RATE
            and rates['date_rate'] >= MIN_DATE_RATE
            and rates['resolved_rate'] >= MIN_RESOLVED_RATE
            and rates['ticker_rate'] >= MIN_TICKER_RATE
        ),
    }
