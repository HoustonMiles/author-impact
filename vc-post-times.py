"""
Recover exact publication timestamps using VoidCrawl as the only fetcher.

WHY VOIDCRAWL AND NOT requests
Several sources in this corpus refuse plain HTTP clients: Substack 403s urllib outright,
and Behind the Balance Sheet and Capital Allocators 403 requests. VoidCrawl drives real
Chrome, so it carries a genuine fingerprint, TLS profile, and session. One fetcher for
every source means one place to set rate limits and identify ourselves.

HOW IT WORKS
Rather than navigating to each URL, this runs fetch() *inside the page*. The request then
uses Chrome's own network stack with cookies and session intact — indistinguishable from
the site's own JavaScript calling its API. One tab serves the whole archive.

    await tab.navigate(root)                      # establish session once
    await tab.evaluate_js("fetch('/api/v1/...').then(r => r.text())")

WHAT THIS DOES NOT REPLACE
Fetching and parsing are different jobs. VoidCrawl gets the bytes; feedparser and
json.loads read them. RSS is XML with a fixed schema and feedparser handles its edge
cases (dc:creator vs author, Blogger's noreply@ wrapper, malformed entities) correctly.
Pointing an LLM at structured XML would be slower, costlier and less accurate. Yosoi
earns its place on unstructured HTML with no feed and no API — that is a different task.

Usage:
    python vc_post_times.py https://thebearcave.substack.com
    python vc_post_times.py https://www.netinterest.co --also-rss

Writes post_times.csv: url, slug, source, published_at (UTC ISO), title.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone

PAGE_SIZE = 50
DELAY = 0.5
NETWORK_IDLE_TIMEOUT = 15.0


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


# Runs in the page. Returns the raw body as text so the caller decides how to parse it.
_FETCH_JS = """
(async () => {{
  try {{
    const r = await fetch({url!r}, {{credentials: 'same-origin'}});
    return JSON.stringify({{ok: r.ok, status: r.status, body: await r.text()}});
  }} catch (e) {{
    return JSON.stringify({{ok: false, status: 0, body: '', error: String(e)}});
  }}
}})()
"""


def _unwrap(raw):
    """evaluate_js may return a JSON string or an already-decoded object."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


async def page_fetch(tab, url: str) -> tuple[int, str]:
    """Fetch a URL from inside the page. Returns (status, body)."""
    result = _unwrap(await tab.evaluate_js(_FETCH_JS.format(url=url)))
    if not result:
        return 0, ''
    return int(result.get('status') or 0), result.get('body') or ''


async def collect(root: str, limit: int | None, also_rss: bool) -> list[dict]:
    from voidcrawl import BrowserConfig, BrowserPool, PoolConfig

    config = PoolConfig(
        browsers=1,
        tabs_per_browser=1,
        browser=BrowserConfig(headless=True, stealth=True,
                              stepping=False, highlight=False),
    )

    rows: list[dict] = []

    async with BrowserPool(config) as pool:
        async with pool.acquire() as tab:
            # Land on the site once so the session, cookies and origin are real.
            await tab.navigate(root)
            try:
                await tab.wait_for_network_idle(timeout=NETWORK_IDLE_TIMEOUT)
            except Exception:  # noqa: BLE001 - idle is best effort
                pass

            offset = 0
            while True:
                api = (f'{root}/api/v1/archive?sort=new&search='
                       f'&offset={offset}&limit={PAGE_SIZE}')
                status, body = await page_fetch(tab, api)

                if status != 200:
                    where = 'first request' if offset == 0 else f'offset {offset}'
                    print(f'  HTTP {status} at {where}'
                          + (' — no archive API on this site' if offset == 0 else ''))
                    break
                try:
                    batch = json.loads(body)
                except json.JSONDecodeError:
                    print('  response was not JSON — not a Substack-style archive API')
                    break
                if not batch:
                    break

                for post in batch:
                    rows.append({
                        'url': post.get('canonical_url', ''),
                        'slug': post.get('slug', ''),
                        'source': 'api',
                        'published_at': iso(post.get('post_date')
                                            or post.get('published_at')) or '',
                        'title': (post.get('title') or '')[:90],
                    })

                print(f'  offset {offset}: {len(batch)} posts')
                offset += len(batch)
                if len(batch) < PAGE_SIZE or (limit and offset >= limit):
                    break
                await asyncio.sleep(DELAY)

            # Optional: pull the feed through the same browser and parse it properly.
            # This is the cross-check that validated lastmod earlier — same fetcher,
            # correct parser for the format.
            if also_rss:
                import feedparser
                status, body = await page_fetch(tab, f'{root}/feed')
                if status == 200:
                    feed = feedparser.parse(body.encode('utf-8'))
                    for e in feed.entries:
                        st = e.get('published_parsed')
                        if not st:
                            continue
                        dt = datetime(*st[:6], tzinfo=timezone.utc)
                        rows.append({
                            'url': (e.get('link') or '').rstrip('/'),
                            'slug': (e.get('link') or '').rstrip('/').split('/')[-1],
                            'source': 'rss',
                            'published_at': dt.isoformat(),
                            'title': (e.get('title') or '')[:90],
                        })
                    print(f'  rss: {len(feed.entries)} entries')
                else:
                    print(f'  rss: HTTP {status}')

    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('site', help='publication root, e.g. https://thebearcave.substack.com')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--also-rss', action='store_true',
                    help='also pull the feed through the browser, to cross-check the API')
    args = ap.parse_args()

    root = args.site.rstrip('/')
    print(f'reading {root} via VoidCrawl...')
    rows = asyncio.run(collect(root, args.limit, args.also_rss))

    if not rows:
        sys.exit('Nothing returned.')

    with open('post_times.csv', 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(
            fh, fieldnames=['url', 'slug', 'source', 'published_at', 'title'])
        w.writeheader()
        w.writerows(rows)

    api_rows = [r for r in rows if r['source'] == 'api']
    withtime = [r for r in api_rows if r['published_at']]

    print(f'\n  posts from API   : {len(api_rows)}')
    if api_rows:
        print(f'  with exact time  : {len(withtime)}  '
              f'({len(withtime) / len(api_rows):.0%})')
    if withtime:
        clock = sorted({r['published_at'][11:16] for r in withtime})
        print(f'  distinct clock times: {len(clock)}')
        print(f'    {", ".join(clock) if len(clock) <= 6 else clock[0] + " to " + clock[-1]} UTC')
        days = sorted(r['published_at'][:10] for r in withtime)
        print(f'  date range       : {days[0]} to {days[-1]}')

    # If both sources ran, report agreement on the overlap.
    if args.also_rss:
        api_by = {r['url'].rstrip('/'): r['published_at'] for r in api_rows}
        pairs = [(u, api_by[u], r['published_at'])
                 for r in rows if r['source'] == 'rss'
                 for u in [r['url']] if u in api_by and api_by[u]]
        if pairs:
            same = sum(1 for _, a, b in pairs if a[:16] == b[:16])
            print(f'\n  API vs RSS overlap: {len(pairs)} posts, '
                  f'{same} agree to the minute')
            for u, a, b in pairs:
                if a[:16] != b[:16]:
                    print(f'    MISMATCH {u}\n      api {a}\n      rss {b}')

    print('\n  wrote post_times.csv')


if __name__ == '__main__':
    main()
