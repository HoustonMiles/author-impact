"""
Measure the error in sitemap <lastmod> as a proxy for publication date.

The pilot event study dates each report by its sitemap <lastmod>, which is a
MODIFICATION date, not a publication date. RSS carries the true pubDate, but only for
the ~20 most recent posts. Overlapping the two gives an empirical error distribution
on that recent window.

    python date_check.py                                   # The Bear Cave
    python date_check.py https://www.netinterest.co         # any Substack-style site

Why it matters:
  * Random error  -> attenuation. The true effect is at least as strong as measured.
  * Systematic LATE lastmod (edits after publication) -> the real event precedes the
    recorded date, and the effect should leak into the pre-event window.
  * Systematic EARLY -> not really possible; lastmod >= publication by construction.

So the sign of the error matters more than its size. Reports here as a signed
distribution, not just a mean absolute error.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import feedparser
import requests

UA = 'Mozilla/5.0 (compatible; CascadingLabs-research/0.1; +https://cascadinglabs.com)'
NS = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def sitemap_dates(root_url: str) -> dict[str, str]:
    resp = requests.get(f'{root_url}/sitemap.xml', headers={'User-Agent': UA}, timeout=60)
    resp.raise_for_status()
    tree = ET.fromstring(resp.content)
    out = {}
    for url in tree.findall('s:url', NS):
        loc = url.findtext('s:loc', default='', namespaces=NS)
        mod = url.findtext('s:lastmod', default='', namespaces=NS)
        if loc and mod:
            out[loc.rstrip('/')] = mod[:10]
    return out


def rss_dates(root_url: str) -> dict[str, tuple[str, str]]:
    """url -> (publication date, publication time) in UTC."""
    resp = requests.get(f'{root_url}/feed', headers={'User-Agent': UA}, timeout=60)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    out = {}
    for e in feed.entries:
        link = (e.get('link') or '').rstrip('/')
        st = e.get('published_parsed') or e.get('updated_parsed')
        if not link or not st:
            continue
        dt = datetime(*st[:6], tzinfo=timezone.utc)
        out[link] = (dt.date().isoformat(), dt.time().isoformat(timespec='minutes'))
    return out


def main() -> None:
    root = (sys.argv[1] if len(sys.argv) > 1
            else 'https://thebearcave.substack.com').rstrip('/')
    print(f'checking {root}\n')

    sm, rss = sitemap_dates(root), rss_dates(root)
    overlap = sorted(set(sm) & set(rss))
    if not overlap:
        sys.exit('No overlapping URLs between sitemap and feed.')

    print(f'{"slug":48} {"lastmod":>11} {"pubDate":>11} {"time":>7} {"delta":>6}')
    deltas = []
    for url in overlap:
        mod = sm[url]
        pub, tm = rss[url]
        d = (datetime.fromisoformat(mod) - datetime.fromisoformat(pub)).days
        deltas.append(d)
        flag = '' if d == 0 else ('  <-- LATE' if d > 0 else '  <-- EARLY?')
        slug = url.split('/p/')[-1][:46]
        print(f'{slug:48} {mod:>11} {pub:>11} {tm:>7} {d:+6d}{flag}')

    n = len(deltas)
    exact = sum(1 for d in deltas if d == 0)
    late = sum(1 for d in deltas if d > 0)
    early = sum(1 for d in deltas if d < 0)
    within1 = sum(1 for d in deltas if abs(d) <= 1)

    print(f"""
SUMMARY  (n = {n})
  exact match          {exact:3d}  ({exact / n:.0%})
  within 1 day         {within1:3d}  ({within1 / n:.0%})
  lastmod LATE  (>0)   {late:3d}  ({late / n:.0%})
  lastmod EARLY (<0)   {early:3d}  ({early / n:.0%})
  mean delta           {sum(deltas) / n:+.2f} days
  max delta            {max(deltas, key=abs):+d} days

  A high exact-match rate means lastmod is a usable publication proxy on this window,
  and remaining error is attenuation (conservative). A high LATE rate means the true
  event precedes the recorded date, and the event study should be re-run with the
  window shifted earlier.

  NOTE: this only covers the ~20 most recent posts, which are also the LEAST likely to
  have been edited. It is an optimistic bound on accuracy, not a representative sample.
  The 2023 migration that overwrote older dates cannot be assessed this way at all.""")

    times = {t for _, t in rss.values()}
    if len(times) <= 3:
        print(f'\n  Publication times are near-constant: {sorted(times)}')
        print('  A fixed schedule means intraday event timing is known without '
              'fetching article pages.')


if __name__ == '__main__':
    main()
