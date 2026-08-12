"""
Harvest candidate sources from directory pages using VoidCrawl. No LLM, no parser.

A directory page is a pile of <a> tags. Getting them needs a rendered DOM, not a model.
VoidCrawl renders the page in Chrome, then we pull every anchor with one eval_js call —
so React lists (Substack leaderboards, /recommendations pages) work, unlike plain HTTP.

Usage:
    python harvest_links.py harvest_pages.txt

Outputs:
    candidates.csv - every external link found, with anchor text and source page
    feeds.txt      - deduped feed URLs, most-linked first, ready for screen_feeds.py

Requires Chrome/Chromium on the system. Tune SKIP_HOSTS / SKIP_PATH_WORDS when junk
shows up in candidates.csv. Over-collect: screen_feeds.py is cheap and discards misses.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

OUT_DIR = Path.cwd()

# Prefix a line in harvest_pages.txt with "follow:" to do a two-hop harvest: collect
# same-domain post URLs from that page, then pull external links out of each post.
# Needed for linkfest blogs (abnormalreturns.com) whose homepage only has permalinks.
FOLLOW_PREFIX = 'follow:'
MAX_FOLLOW_PAGES = 25   # cap the second hop; these are small sites

SCROLL_PASSES = 6      # lazy-loaded lists need a nudge; set 0 to disable
SCROLL_PAUSE = 0.8
NETWORK_IDLE_TIMEOUT = 15.0

SKIP_HOSTS = {
    'twitter.com', 'x.com', 'facebook.com', 'linkedin.com', 'youtube.com',
    'instagram.com', 'reddit.com', 'apple.com', 'podcasts.apple.com', 'spotify.com',
    'open.spotify.com', 'google.com', 'github.com', 'wikipedia.org', 'amazon.com',
    'bsky.app', 'threads.net', 'tiktok.com', 'wordpress.com', 'gravatar.com',
    # data terminals, newsletter platforms, podcast hosts, paywalled majors -
    # all appeared in a real harvest and none are an author-authored source
    'bloomberg.com', 'koyfin.com', 'finchat.io', 'atom.finance', 'bamsec.com',
    'tradingview.com', 'de.tradingview.com', 'beehiiv.com', 'pod.link',
    'amibroker.com', 'oaktreecapital.com', 'ritholtzwealth.com', 'benjaminai.co',
    # ad-tech / trackers that leak out of blog templates
    '3lift.com', 'eb2.3lift.com', 'doubleclick.net', 'googletagmanager.com',
    'scorecardresearch.com', 'quantserve.com', 'outbrain.com', 'taboola.com',
    # mainstream media and read-later services - linkfests cite these constantly,
    # they are never author-authored ticker analysis, and several block or 403
    'nytimes.com', 'wsj.com', 'ft.com', 'reuters.com', 'cnbc.com', 'bbc.com',
    'washingtonpost.com', 'wapo.st', 'npr.org', 'axios.com', 'theatlantic.com',
    'slate.com', 'nymag.com', 'wired.com', 'engadget.com', 'arstechnica.com',
    'theguardian.com', 'economist.com', 'businessinsider.com', 'forbes.com',
    'marketwatch.com', 'barrons.com', 'fortune.com', 'time.com', 'vox.com',
    'getpocket.com', 'instapaper.com', 'tinyurl.com', 'bit.ly', 'stocktwits.com',
    'papers.ssrn.com', 'theconversation.com', 'sciencedaily.com', 'morningstar.com',
}

SKIP_PATH_WORDS = (
    '/privacy', '/terms', '/about', '/contact', '/login', '/signin', '/signup',
    '/subscribe', '/cdn-cgi/', '/tag/', '/category/', '/author/', '/feed',
    '/comments', '/share', '/api/', '/leaderboard', '/browse',
)

# Pull every anchor's text + resolved href from the live DOM in one round trip.
# a.href is already absolute in the browser, so urljoin is only a safety net.
_EXTRACT_JS = """
(() => JSON.stringify(
  Array.from(document.querySelectorAll('a[href]')).map(a => ({
    text: (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
    href: a.href
  }))
))()
"""


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def is_candidate(url: str, source_host: str) -> bool:
    if not url.startswith(('http://', 'https://')):
        return False
    host = host_of(url)
    if not host or host == source_host or host in SKIP_HOSTS:
        return False
    # substack.com/@handle and /leaderboard are chrome; foo.substack.com is a publication
    if host == 'substack.com':
        return False
    low = url.lower()
    return not any(w in low for w in SKIP_PATH_WORDS)


def post_links(anchors: list[dict], page: str) -> list[str]:
    """Same-domain links that look like individual posts, for the second hop."""
    src_host = host_of(page)
    out, seen = [], set()
    for a in anchors:
        url = urljoin(page, (a.get('href') or '').strip())
        if not url.startswith(('http://', 'https://')):
            continue
        if host_of(url) != src_host:
            continue
        path = urlparse(url).path.rstrip('/')
        # a post has real path depth; '/', '/page/2', '/about' do not
        if path.count('/') < 2 or any(w in url.lower() for w in SKIP_PATH_WORDS):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def feed_url(host: str) -> str:
    return f'https://{host}/feed'


def _coerce(raw) -> list[dict]:
    """eval_js may hand back a JSON string or an already-decoded object."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def rows_from_anchors(anchors: list[dict], page: str) -> list[dict]:
    """Pure function so the filtering can be tested without a browser."""
    src_host = host_of(page)
    out = []
    for a in anchors:
        url = urljoin(page, (a.get('href') or '').strip())
        if not is_candidate(url, src_host):
            continue
        out.append({
            'source_page': page,
            'anchor_text': (a.get('text') or '').strip(),
            'link': url,
            'host': host_of(url),
        })
    return out


async def harvest(pages: list[str], follow_set: set[str] | None = None) -> list[dict]:
    # Import name is `voidcrawl`. There is no BrowserPool.from_env() in the release —
    # construct a PoolConfig explicitly. Note BrowserConfig defaults stepping/highlight
    # to True (demo aids); both are off here or every page pays a step delay.
    from voidcrawl import BrowserConfig, BrowserPool, PoolConfig

    rows: list[dict] = []
    follow_set = follow_set or set()

    config = PoolConfig(
        browsers=1,
        tabs_per_browser=2,
        browser=BrowserConfig(
            headless=True,
            stealth=True,
            stepping=False,
            highlight=False,
        ),
    )

    async with BrowserPool(config) as pool:
        for page in pages:
            try:
                async with pool.acquire() as tab:
                    await tab.navigate(page)
                    try:
                        await tab.wait_for_network_idle(timeout=NETWORK_IDLE_TIMEOUT)
                    except Exception:  # noqa: BLE001 - idle is best-effort
                        pass

                    for _ in range(SCROLL_PASSES):
                        await tab.evaluate_js(
                            'window.scrollTo(0, document.body.scrollHeight)'
                        )
                        await asyncio.sleep(SCROLL_PAUSE)

                    anchors = _coerce(await tab.evaluate_js(_EXTRACT_JS))
            except Exception as exc:  # noqa: BLE001
                print(f'  FAIL  {page}: {type(exc).__name__}: {exc}')
                continue

            found = rows_from_anchors(anchors, page)
            rows.extend(found)
            print(f'  {"ok  " if found else "EMPTY"} {page}: {len(found)} external '
                  f'links (of {len(anchors)} anchors)')

            if page not in follow_set:
                continue

            # Second hop: this page is an index of posts, and the outbound links
            # we want live inside those posts rather than on the index itself.
            posts = post_links(anchors, page)[:MAX_FOLLOW_PAGES]
            print(f'         following {len(posts)} same-domain posts...')
            for post in posts:
                try:
                    async with pool.acquire() as tab:
                        await tab.navigate(post)
                        try:
                            await tab.wait_for_network_idle(
                                timeout=NETWORK_IDLE_TIMEOUT
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        sub = _coerce(await tab.evaluate_js(_EXTRACT_JS))
                except Exception as exc:  # noqa: BLE001
                    print(f'         FAIL {post}: {type(exc).__name__}')
                    continue
                got = rows_from_anchors(sub, post)
                rows.extend(got)
                print(f'         +{len(got):3d} {post}')

    return rows


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    pages, follow_set = [], set()
    for ln in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#'):
            continue
        if ln.startswith(FOLLOW_PREFIX):
            ln = ln[len(FOLLOW_PREFIX):].strip()
            follow_set.add(ln)
        pages.append(ln)

    print(f'Harvesting {len(pages)} directory pages '
          f'({len(follow_set)} with post-following)...\n')

    rows = asyncio.run(harvest(pages, follow_set))

    if rows:
        with (OUT_DIR / 'candidates.csv').open('w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    counts = Counter(r['host'] for r in rows)
    names: dict[str, str] = {}
    for r in rows:
        if r['anchor_text'] and r['host'] not in names:
            names[r['host']] = r['anchor_text'][:60]

    # MERGE with any existing feeds.txt rather than clobbering it. Harvest runs are
    # incremental - you add a directory page at a time - and overwriting silently
    # discards every source found on previous runs.
    existing: dict[str, int] = {}
    feeds_path = OUT_DIR / 'feeds.txt'
    if feeds_path.exists():
        for line in feeds_path.read_text(encoding='utf-8').splitlines():
            url = line.split('#', 1)[0].strip()
            if not url:
                continue
            host = host_of(url)
            # Apply the CURRENT skip list to pre-existing entries too, otherwise
            # hosts banned after they were first harvested live on forever in the file.
            if not host or host in SKIP_HOSTS:
                continue
            prior = 0
            if '# x' in line:
                head = line.split('# x', 1)[1].split()[0]
                if head.isdigit():
                    prior = int(head)
            existing[host] = max(existing.get(host, 0), prior)

    merged = Counter(existing)
    for host, n in counts.items():
        merged[host] = max(merged.get(host, 0), n)

    added = sorted(set(counts) - set(existing))

    lines = [
        '# Auto-harvested via VoidCrawl, ordered by inbound link count.',
        '# Merged across runs; feed paths are GUESSES that screen_feeds.py verifies.',
    ]
    lines += [
        f'{feed_url(h)}    # x{n} {names.get(h, "")}'.rstrip()
        for h, n in merged.most_common()
    ]
    feeds_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print(f"""
--- SUMMARY ---
external links   : {len(rows)}
hosts this run   : {len(counts)}
new this run     : {len(added)}
total in feeds   : {len(merged)}

  candidates.csv -> every link, with anchor text and source page
  feeds.txt      -> deduped feeds, most-linked first; run screen_feeds.py next

Diagnostics: 0 external links but a high anchor count means the filters are too
aggressive (check SKIP_PATH_WORDS). An anchor count near 0 means the page never
rendered - raise SCROLL_PASSES or NETWORK_IDLE_TIMEOUT.
""")


if __name__ == '__main__':
    main()
