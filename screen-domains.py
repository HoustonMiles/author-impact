"""
Feasibility screen for the author-attribution study.

Runs one Yosoi contract across a list of candidate index/archive pages and answers
three questions in a single pass:

  1. DOMAIN SCREEN   - which domains actually expose an author and a publish time?
  2. BYLINE INVENTORY - what do the raw bylines look like, and how dirty are they?
  3. AUTHOR CANDIDATES - which normalized names appear on 2+ domains? (your 20-author test)

Usage:
    python screen_domains.py domains.txt

where domains.txt has one URL per line (archive / index / "latest articles" pages),
blank lines and #-comments ignored.

Outputs three CSVs next to the script plus a printed summary with a go/no-go read.

NOTE ON TIMEZONES: ys.Datetime defaults to assume_utc=True, which silently reads an
unlabeled local timestamp as UTC. For a lead-lag study that is fatal - it reorders who
published first. This script sets assume_utc=False and records anything that fails to
parse, so you can see per-domain how many timestamps are ambiguous before you trust them.
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import yosoi as ys
from yosoi.models.selectors import SelectorEntry

OUT_DIR = Path.cwd()  # write reports where you run it, not next to the script

# provider:model-name. Discovery runs ONCE per domain and is then cached, so this
# is roughly one call per domain, not one per scrape - pick for quality, not price.
MODEL = 'gemini:gemini-3.6-flash'

# Substack/React archives serve an empty shell over plain HTTP: 'auto' sees a
# successful fetch, never escalates, and discovery gets HTML with no posts in it.
# Force headless so DOMLoader scrolls and collects the list. Accepts a dict for
# per-domain control, e.g. {'fool.com': 'simple', 'default': 'headless'}.
FETCHER = 'headless'

# True = let Yosoi print its discovery/verification steps. Turn this on the moment
# anything returns 0 records; the useful error is always in that output.
VERBOSE = False

# Screening thresholds - tune these, but set them BEFORE you look at results.
MIN_ITEMS = 5          # a domain must yield at least this many articles to judge it
MIN_AUTHOR_RATE = 0.7  # fraction of items with a non-null author
MIN_DATE_RATE = 0.7
MIN_RESOLVED_RATE = 0.5   # byline must resolve to a PERSON, not just be non-empty
MIN_TICKER_RATE = 0.15    # no tickers -> no lead rate -> useless, however good the writing    # fraction of items with a parseable publish time
GO_DOMAIN_COUNT = 40   # how many passing domains you decided you need


class ArticleRecord(ys.Contract):
    """One article as listed on an index/archive page."""

    # Without a root selector Yosoi extracts ONE record for the whole page. This
    # sentinel tells discovery to find the repeating article-card container, so we
    # get one record per article instead of one per page.
    root: ClassVar[SelectorEntry | None] = ys.discover()

    # EVERY field is optional. This is a screening tool: a field it cannot find is a
    # measurement (that domain scores badly), not a fatal error. A required field makes
    # discovery abort and return zero records, which destroys the signal we came for.
    headline: str | None = ys.Title(description='Article headline or title')
    author: str | None = ys.Author(description='Byline / author name, exactly as printed')
    published_at: str | None = ys.Datetime(
        description='Publication date and time as printed on the page',
        assume_utc=False,   # do NOT silently stamp unlabeled local times as UTC
        past_only=True,     # reject hallucinated future dates
    )
    article_url: str | None = ys.Url(description='Link to the full article')


# --------------------------------------------------------------------------------------
# Byline normalization
# --------------------------------------------------------------------------------------

# Yosoi's Author coercion only strips whitespace, so bylines arrive dirty: prefixes,
# glued-on dates, outlet names, separators. Normalize before comparing across domains.

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


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


# --------------------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------------------

async def collect(urls: list[str]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Scrape each candidate page independently so one bad domain can't kill the run.

    ys.scrape returns a ScrapeResult envelope, NOT a list of items. The extracted
    rows live at result.results[i].records and are plain dicts.
    """
    by_domain: dict[str, list[dict]] = defaultdict(list)
    errors: dict[str, str] = {}

    for url in urls:
        dom = domain_of(url)
        try:
            result = await ys.scrape(
                url,
                ArticleRecord,
                fetcher_type=FETCHER,
                policy=ys.Policy(model=ys.ModelPolicy.from_string(MODEL)),
                quiet=not VERBOSE,
            )
        except Exception as exc:  # noqa: BLE001 - record the failure, don't raise
            print(f'  FAIL  {dom}: {type(exc).__name__}: {exc}')
            errors[dom] = f'{type(exc).__name__}: {exc}'
            by_domain[dom] = []
            continue

        units = getattr(result, 'results', None)
        if units is None:  # defensive: older/other return shape
            print(f'  ????  {dom}: unexpected return type {type(result).__name__}')
            errors[dom] = f'unexpected return type {type(result).__name__}'
            by_domain[dom] = []
            continue

        n_before = len(by_domain[dom])
        for unit in units:
            if unit.status != 'ok':
                errors[dom] = unit.error or f'status={unit.status}'
            # A record with no usable field is a discovery artifact, not an article.
            by_domain[dom].extend(
                r for r in unit.records
                if any(r.get(k) for k in ('headline', 'author', 'published_at', 'article_url'))
            )
            if unit.quality_issues:
                errors.setdefault(dom, '; '.join(unit.quality_issues))

        got = len(by_domain[dom]) - n_before
        note = f'  ({errors[dom]})' if dom in errors else ''
        print(f'  ok    {dom}: {got} records{note}')

    return by_domain, errors


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def write_reports(
    by_domain: dict[str, list[dict]],
    errors: dict[str, str],
) -> tuple[int, int]:
    domain_rows = []
    byline_rows = []
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
            tks = tickers_in(i.get('headline'))
            if tks:
                n_ticker += 1
            byline_rows[-1]['tickers'] = ' '.join(sorted(tks))
            if norm:
                n_resolved += 1
                normalized_here.add(norm)

        for norm in normalized_here:
            name_to_domains[norm].add(dom)

        author_rate = n_author / n if n else 0.0
        date_rate = n_date / n if n else 0.0
        passes = (
            n >= MIN_ITEMS
            and author_rate >= MIN_AUTHOR_RATE
            and date_rate >= MIN_DATE_RATE
            and (n_resolved / n if n else 0) >= MIN_RESOLVED_RATE
            and (n_ticker / n if n else 0) >= MIN_TICKER_RATE
        )

        domain_rows.append({
            'domain': dom,
            'items': n,
            'author_rate': round(author_rate, 3),
            'date_rate': round(date_rate, 3),
            'resolved_rate': round(n_resolved / n, 3) if n else 0.0,
            'ticker_rate': round(n_ticker / n, 3) if n else 0.0,
            'distinct_authors': len(normalized_here),
            'passes': passes,
            'error': errors.get(dom, ''),
        })

    # Candidate cross-domain identities - the automated version of the 20-author test
    candidate_rows = [
        {
            'normalized_name': name,
            'n_domains': len(doms),
            'domains': '; '.join(sorted(doms)),
        }
        for name, doms in sorted(name_to_domains.items(), key=lambda kv: -len(kv[1]))
        if len(doms) >= 2
    ]

    _dump('domain_report.csv', domain_rows)
    _dump('bylines.csv', byline_rows)
    _dump('author_candidates.csv', candidate_rows)

    n_pass = sum(1 for r in domain_rows if r['passes'])
    return n_pass, len(candidate_rows)


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


def preflight() -> None:
    """Fail fast on a missing API key instead of reporting 0 records for every domain."""
    from dotenv import load_dotenv

    load_dotenv()
    provider = MODEL.split(':', 1)[0]
    candidates = {
        'gemini': ('GEMINI_API_KEY', 'GEMINI_KEY', 'GOOGLE_API_KEY'),
        'google': ('GEMINI_API_KEY', 'GEMINI_KEY', 'GOOGLE_API_KEY'),
        'groq': ('GROQ_API_KEY', 'GROQ_KEY'),
        'openai': ('OPENAI_API_KEY', 'OPENAI_KEY'),
        'anthropic': ('ANTHROPIC_API_KEY',),
        'openrouter': ('OPENROUTER_API_KEY', 'OPENROUTER_KEY'),
    }.get(provider, ())

    if candidates and not any(os.getenv(v) for v in candidates):
        sys.exit(
            f"\nNo API key found for provider '{provider}'.\n"
            f'Set one of: {", ".join(candidates)}\n'
            f'Either export it, or put it in a .env file in THIS directory '
            f'({Path.cwd()}) — load_dotenv() reads from the working directory, '
            f'not from wherever Yosoi is installed.\n'
        )


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    preflight()

    src = Path(sys.argv[1])
    # Strip INLINE comments too - '#' is a URL fragment delimiter, so leaving an
    # annotation attached corrupts the path silently rather than erroring cleanly.
    urls = [
        u for u in (
            line.split('#', 1)[0].strip()
            for line in src.read_text(encoding='utf-8').splitlines()
        ) if u
    ]
    print(f'Screening {len(urls)} candidate pages...\n')

    by_domain, errors = asyncio.run(collect(urls))
    print()
    n_pass, n_candidates = write_reports(by_domain, errors)

    print(f"""
--- SUMMARY ---
domains attempted : {len(by_domain)}
domains passing   : {n_pass}   (>={MIN_ITEMS} items, >={MIN_AUTHOR_RATE:.0%} author, >={MIN_DATE_RATE:.0%} date)
cross-domain names: {n_candidates}

Read:
  domain_report.csv     -> which sources are usable at all
  bylines.csv           -> how dirty the author strings are; check normalization by eye
  author_candidates.csv -> names on 2+ domains; hand-verify these, that IS the 20-author test

Go/no-go: you decided you need ~{GO_DOMAIN_COUNT} passing domains. You have {n_pass}.
""")


if __name__ == '__main__':
    main()
