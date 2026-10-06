"""Fetch player cards (name, OVR, position, stats, card image keys) from renderz.app.

Standalone and read-only with respect to the bot: it never imports ``database`` and
writes only the JSONL file given by ``--out`` (and, with ``--cards``, one PNG per player).

It reads the public, server-rendered ``/players?page=N`` list (robots.txt allows it;
``/api/*`` and ``?sort=`` URLs are disallowed and never used). Polite by default:
one request at a time, ``--delay`` seconds between pages, resumable via the output
file (pages already present are skipped), stops on the first non-200 answer.

    python scripts/fetch_renderz_players.py --out renderz_players.jsonl --pages 1-3
    python scripts/fetch_renderz_players.py --out renderz_players.jsonl --ovr-min 110
    python scripts/fetch_renderz_players.py --out renderz_players.jsonl --pages 1-2 --cards cards/

``--cards`` saves each player's FULL card (frame, portrait, OVR, position, name, flag,
club, badges) as ``<player id>.png``. The site draws cards in the browser from layers,
so this needs a real browser: Playwright with an installed Edge/Chrome (or its own
Chromium), ``pip install playwright``. Signed image URLs are never stored.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://renderz.app"
USER_AGENT = "Mozilla/5.0 (compatible; logovobot-research/1.0; +https://github.com/sp1r1tVSA/logovobot)"

ROW_SPLIT = re.compile(r'(?=<a class="group flex min-h-\[104px\])')
HREF = re.compile(r'href="/player/(\d+)-([^"]+)"')
ARIA = re.compile(r'aria-label="([^"]*)"')
RATING = re.compile(r'class="rating[^"]*"[^>]*>(?:(?!</div>).)*?<span[^>]*>(\d+)</span>', re.S)
POSITION = re.compile(r'class="position[^"]*"[^>]*>([^<]+)<')
STAT = re.compile(
    r'tabular-nums text-white">\s*(\d+)\s*(?:<!--.*?-->)*\s*</span>\s*'
    r'<span[^>]*>([A-Z]{3})</span>',
    re.S,
)
def parse_rows(page_html: str) -> list[dict]:
    rows = []
    for chunk in ROW_SPLIT.split(page_html):
        m = HREF.search(chunk[:600])
        if not m:
            continue
        head = chunk[:600]
        name = ARIA.search(head)
        rating = RATING.search(chunk)
        pos = POSITION.search(chunk)
        rows.append({
            "id": int(m.group(1)),
            "slug": m.group(2),
            "name": html.unescape(name.group(1)) if name else None,
            "ovr": int(rating.group(1)) if rating else None,
            "position": pos.group(1).strip() if pos else None,
            "stats": {label: int(val) for val, label in STAT.findall(chunk)},
        })
    return rows


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


HIDE_OVERLAYS_JS = """() => {
  for (const n of document.querySelectorAll('body *')) {
    const pos = getComputedStyle(n).position;
    if ((pos === 'fixed' || pos === 'sticky') && !n.querySelector('[data-player-card]') && !n.closest('[data-player-card]'))
      n.style.setProperty('display', 'none', 'important');
  }
  for (const e of [document.documentElement, document.body]) e.style.setProperty('background', 'transparent', 'important');
}"""


def _launch(pw):
    """Prefer an installed Edge/Chrome (no download needed), else Playwright's Chromium."""
    for channel in ("msedge", "chrome", None):
        try:
            return pw.chromium.launch(channel=channel) if channel else pw.chromium.launch()
        except Exception:
            continue
    raise RuntimeError("no browser: install Edge/Chrome or run `playwright install chromium`")


def _settle(page) -> None:
    """Grow the viewport to the whole list (the page scrolls inside <body>, so cards below
    the fold never render), then wait until every card image has loaded."""
    height = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
    page.set_viewport_size({"width": 1280, "height": min(int(height) + 200, 5200)})
    page.wait_for_timeout(1200)
    page.wait_for_function(
        "[...document.querySelectorAll('[data-player-card] img')].every(i => i.complete && i.naturalWidth > 0)",
        timeout=20000,
    )


def _blank(path: str) -> bool:
    from PIL import Image
    im = Image.open(path).convert("RGB").resize((24, 24))
    px = list(im.getdata())
    return sum(1 for p in px if sum(p) < 40) / len(px) > 0.5


def _shoot(page, card, dest: str) -> bool:
    """Screenshot one card; a blank or half-drawn result is retried once after a pause."""
    for _ in range(2):
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        card.screenshot(path=dest, omit_background=True, animations="disabled")
        if not _blank(dest):
            return True
    os.remove(dest)
    return False


def page_url(page: int, ovr_min: int | None) -> str:
    params = {"page": page}
    if ovr_min:
        params["ovr_min"] = ovr_min
    return f"{BASE}/players?{urllib.parse.urlencode(params)}"


def _done_pages(path: str) -> set[int]:
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["page"])
                except (ValueError, KeyError):
                    continue
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="JSONL file; one line per player, resumable")
    ap.add_argument("--pages", default="1-3", help="page range, e.g. 1-3 or 5 (default 1-3)")
    ap.add_argument("--ovr-min", type=int, default=None, help="server-side OVR filter")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between pages (default 2)")
    ap.add_argument("--cards", default=None, help="folder for full-card PNGs (<id>.png); needs a browser")
    ap.add_argument("--scale", type=int, default=3, help="screenshot scale, 3 gives ~256 px cards (default 3)")
    args = ap.parse_args()

    lo, _, hi = args.pages.partition("-")
    pages = range(int(lo), int(hi or lo) + 1)
    done = _done_pages(args.out)
    todo = [n for n in pages if n not in done]
    for n in pages:
        if n in done:
            print(f"page {n}: already in {args.out}, skipped")
    if not todo:
        return 0
    if args.cards:
        os.makedirs(args.cards, exist_ok=True)

    total = 0
    pw_cm = browser = page = None
    if args.cards:
        from playwright.sync_api import sync_playwright
        pw_cm = sync_playwright().start()
        browser = _launch(pw_cm)
        page = browser.new_page(viewport={"width": 1280, "height": 1400}, device_scale_factor=args.scale)
    try:
        with open(args.out, "a", encoding="utf-8") as out:
            for n in todo:
                url = page_url(n, args.ovr_min)
                try:
                    if page:
                        page.goto(url, wait_until="networkidle", timeout=60000)
                        page.wait_for_timeout(1000)
                        rows = parse_rows(page.content())
                    else:
                        rows = parse_rows(fetch(url))
                except urllib.error.HTTPError as exc:
                    print(f"page {n}: HTTP {exc.code}, stopping", file=sys.stderr)
                    return 1
                except Exception as exc:
                    print(f"page {n}: {exc}, stopping", file=sys.stderr)
                    return 1
                if not rows:
                    print(f"page {n}: no cards, end of list")
                    break
                saved = 0
                if page:
                    page.evaluate(HIDE_OVERLAYS_JS)
                    _settle(page)
                    cards = page.locator("[data-player-card]:visible")
                    for i in range(cards.count()):
                        card = cards.nth(i)
                        href = card.evaluate("e => e.closest('a')?.getAttribute('href') || ''")
                        m = HREF.search(f'href="{href}"')
                        dest = os.path.join(args.cards, f"{m.group(1)}.png") if m else None
                        if not dest or os.path.exists(dest):
                            continue
                        if _shoot(page, card, dest):
                            saved += 1
                        else:
                            print(f"  card {m.group(1)}: stayed blank, skipped", file=sys.stderr)
                for row in rows:
                    row["page"] = n
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                total += len(rows)
                print(f"page {n}: {len(rows)} cards" + (f", {saved} images" if page else ""), flush=True)
                time.sleep(args.delay)
    finally:
        if browser:
            browser.close()
        if pw_cm:
            pw_cm.stop()
    print(f"done, {total} new cards -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
