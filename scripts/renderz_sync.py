"""Build the Renderz card base for the 80 clubs of the league.

Standalone and read-only with respect to the bot: it never imports ``database`` and
writes only into ``--out`` (default ``renderz_sync/``). Five resumable stages:

    python scripts/renderz_sync.py tm        # squads of the 80 clubs from Transfermarkt
    python scripts/renderz_sync.py scan      # all reachable Renderz list pages + portraits
    python scripts/renderz_sync.py scan-pos  # position lists, which reach below the main list's OVR 107
    python scripts/renderz_sync.py resolve   # detail pages of ambiguous cards (full name + club)
    python scripts/renderz_sync.py match     # cards <-> Transfermarkt players, dedupe, OVR base
    python scripts/renderz_sync.py cards     # full card PNGs of the selected cards only
    python scripts/renderz_sync.py all       # the stages above in order

Output layout::

    tm_squads.json       {club: {"tm_id", "tm_name", "players": [{name, position, joined, value}]}}
    renderz_rows.jsonl   one line per Renderz card seen (id, ea_id, program, ovr, tradable, ...)
    portraits_raw/       <ea_id>.png  256x256 action shots of cards whose name may match
    card_details.json    {renderz_id: {"full_name", "team"}} card detail pages for ambiguous groups
    ovr_db.json          every card version of every matched player (no stats)
    selected_cards.json  one card per (player, program, ovr): the tradable copy when both exist
    portraits/           <slug>.png named like ``assets/players/`` (``player_photos._slugify``)
    cards/               <renderz id>.png full card images of the selected cards

Squads are taken as of ``--as-of`` (default 2026-09-17): a player whose «Joined» date on the
Transfermarkt squad page is later than that is left out. Players who left after the date are
already gone from the live page — a known limit of reading the current squad.

Cards above ``--ovr-max`` (default 114, the league ceiling) are skipped: the scan bisects
to the first page that has a card at or below it, and the match stage ignores the rest.
Icon and hero cards (program ``*_ICON`` / ``*_HERO``, or an ICON / HERO token in the card background) are skipped too — ``--keep-icons`` brings
them back.
The main Renderz list only reaches page 416 (about 10k cards, OVR >= 107): the site clamps deeper
pages to the last one and ignores ``ovr_min``. The position lists (``/players/position/<pos>``)
are not clamped, so ``scan-pos`` walks them down to ``--ovr-min`` (default 70).

Transfermarkt sits behind a JS challenge, so squads are read through Playwright with an
installed Edge/Chrome (``pip install playwright``); Renderz lists need plain urllib only.
Mind both sites' terms of service: this is for the league's own research use, polite delays
are on by default and nothing is republished.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_renderz_players as rz  # noqa: E402  (list parser + screenshot helpers)

TM = "https://www.transfermarkt.com"
AS_OF = dt.date(2026, 9, 17)
LAST_PAGE = 415          # page 416 and deeper repeat the final page

# Russian club name (as in config.DIVISION_CLUBS) -> Transfermarkt club id.
# tests/test_renderz_sync.py keeps the keys equal to DIVISION_CLUBS.
CLUBS: dict[str, int] = {
    # DIV_1
    "Лидс": 399, "Ренн": 273, "Ницца": 417, "Нэшвилл": 63966, "Порту": 720, "Вест Хэм": 379,
    "Вольфсбург": 82, "Фиорентина": 430, "Лацио": 398, "Марсель": 244, "Лилль": 1082,
    "Айнтрахт": 24, "Майнц": 39, "Бернли": 1132, "Будё Глимт": 501, "Кельн": 3,
    # DIV_2
    "Вулверхэмптон": 543, "Бурирам": 25449, "Валенсия": 1049, "Сельта": 940, "Ривер Плейт": 209,
    "Аякс": 610, "Спортинг": 336, "Монако": 162, "Бенфика": 294, "Фулхэм": 931,
    "Хоффенхайм": 533, "Ланс": 826, "Аль-Кадисия": 26069, "Торино": 416, "Лос Анджелес": 51828,
    "ПСВ": 383,
    # DIV_3
    "Сандерленд": 289, "Ноттингем Форест": 703, "Реал Сосьедад": 681, "Париж": 10004,
    "Фенербахче": 36, "Комо": 1047, "Брентфорд": 1148, "Кристал Пэлас": 873, "Аль-Ахли": 18487,
    "Лион": 1041, "Борнмут": 989, "Аль-Иттихад": 8023, "Трабзонспор": 449, "Вильярреал": 1050,
    "Штутгарт": 79, "Болонья": 1025,
    # DIV_4
    "Байя": 10010, "Милан": 5, "Боруссия Дортмунд": 16, "Интер Милан": 46, "Брайтон": 1237,
    "Байер": 15, "Лейпциг": 23826, "Эвертон": 29, "Аталанта": 800, "Астон Вилла": 405,
    "Бешикташ": 114, "Интер Майами": 69261, "Бетис": 150, "Аль-Хиляль": 1114, "Ньюкасл": 762,
    "Атлетик Бильбао": 621,
    # DIV_5
    "Арсенал": 11, "Манчестер Сити": 281, "Манчестер Юнайтед": 985, "Тоттенхэм": 148,
    "Атлетико Мадрид": 13, "Барселона": 131, "Реал Мадрид": 418, "Бавария": 27, "Ливерпуль": 31,
    "Челси": 631, "Наполи": 6195, "Ювентус": 506, "Рома": 12, "ПСЖ": 583, "Галатасарай": 141,
    "Аль-Наср": 18544,
}

# ---------------------------------------------------------------- names

_FOLD = str.maketrans({"ø": "o", "ß": "ss", "đ": "d", "ł": "l", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th"})
_JR = {"jr": "junior", "sr": "senior"}


def norm_tokens(name: str) -> tuple[str, ...]:
    s = unicodedata.normalize("NFKD", (name or "").lower().translate(_FOLD))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return tuple(_JR.get(t, t) for t in s.split())


def tokens_match(card: tuple[str, ...], player: tuple[str, ...]) -> bool:
    """Every card token equals a player token or (>= 4 letters) is its prefix: «Vini Jr.»."""
    if not card or not player:
        return False
    for t in card:
        if not any(t == p or (len(t) >= 4 and p.startswith(t)) for p in player):
            return False
    return True


# ---------------------------------------------------------------- stage tm

_SQUAD_JS = """() => {
  const tbl = document.querySelector('table.items');
  if (!tbl) return null;
  const heads = [...tbl.querySelectorAll('thead th')].map(th => th.innerText.trim());
  const jIdx = heads.findIndex(h => h.startsWith('Joined'));
  const title = (document.querySelector('h1')?.innerText || '').trim();
  const rows = [];
  for (const tr of tbl.querySelectorAll(':scope > tbody > tr')) {
    const cell = tr.querySelector('td.posrela');
    if (!cell) continue;
    const a = cell.querySelector('td.hauptlink a, .hauptlink a');
    const name = (a ? a.innerText : '').trim();
    const pos = (cell.querySelector('table tr:nth-child(2) td')?.innerText || '').trim();
    const cells = [...tr.children];
    const joined = jIdx >= 0 && cells[jIdx] ? cells[jIdx].innerText.trim() : '';
    const value = (tr.querySelector('td.rechts.hauptlink')?.innerText || '').trim();
    if (name) rows.push({name, position: pos, joined, value});
  }
  return {title, rows};
}"""


def parse_joined(text: str) -> dt.date | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def stage_tm(args) -> int:
    from playwright.sync_api import sync_playwright

    path = os.path.join(args.out, "tm_squads.json")
    result = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    todo = [c for c in CLUBS if c not in result and (not args.club or c in args.club)]
    if not todo:
        print("tm: nothing to do")
        return 0
    with sync_playwright() as pw:
        browser = rz._launch(pw)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        for n, club in enumerate(todo, 1):
            tm_id = CLUBS[club]
            url = f"{TM}/x/kader/verein/{tm_id}/plus/1"   # no saison_id: MLS/Brazil seasons are calendar years
            data = None
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(12):             # the first visit solves the JS challenge
                    try:
                        data = page.evaluate(_SQUAD_JS)
                    except Exception:           # the challenge reloads the page under us
                        data = None
                    if data and data["rows"]:
                        break
                    page.wait_for_timeout(1500)
            except Exception as exc:
                print(f"[{n}/{len(todo)}] {club}: {exc}", file=sys.stderr)
            if not data or not data["rows"]:
                print(f"[{n}/{len(todo)}] {club} ({tm_id}): no squad table, skipped", file=sys.stderr)
                continue
            players, late = [], 0
            for r in data["rows"]:
                joined = parse_joined(r["joined"])
                if joined and joined > args.as_of:
                    late += 1
                    continue
                players.append({"name": r["name"], "position": r["position"],
                                "joined": r["joined"], "value": r["value"]})
            result[club] = {"tm_id": tm_id, "tm_name": data["title"], "players": players}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
            print(f"[{n}/{len(todo)}] {club} = «{data['title']}»: {len(players)} players"
                  f"{f', {late} joined after {args.as_of}' if late else ''}", flush=True)
            time.sleep(args.tm_delay)
        browser.close()
    missing = [c for c in CLUBS if c not in result]
    if missing:
        print("tm: clubs still missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- stage scan

IMG = re.compile(r'<img[^>]*?src="([^"]+)"[^>]*?alt="([^"]*)"')
ACTION = re.compile(r"player_25_(\d+)_([A-Za-z0-9]+?(?:_[A-Za-z0-9]+)*?)_[0-9a-f]{16}\b")
CLUB_IMG = re.compile(r"club_23_(\d+)")
BG_NAME = re.compile(r"/(bg_[^/?]+)")
UNTRADEABLE = "common_23_untradeable_icon"


def parse_card_rows(page_html: str) -> list[dict]:
    """`rz.parse_rows` plus what the sync needs: EA id, program, club image, tradable, portrait url."""
    out = []
    chunks = {}
    for chunk in rz.ROW_SPLIT.split(page_html):
        m = rz.HREF.search(chunk[:600])
        if m:
            chunks[int(m.group(1))] = chunk
    for row in rz.parse_rows(page_html):
        chunk = chunks.get(row["id"], "")
        row.pop("stats", None)
        ea = prog = portrait = bg = None
        club_img = None
        for src, alt in IMG.findall(chunk):
            src = html.unescape(src)
            if alt == "Player Card Background":
                m = BG_NAME.search(src)
                bg = m.group(1) if m else None
            elif alt == "Action shot":
                portrait = src
                m = ACTION.search(src)
                if m:
                    ea, prog = int(m.group(1)), m.group(2)
            elif alt == "Club":
                m = CLUB_IMG.search(src)
                club_img = int(m.group(1)) if m else None
        row.update(ea_id=ea, program=prog, bg=bg, club_img=club_img, tradable=UNTRADEABLE not in chunk,
                   portrait_url=portrait)
        out.append(row)
    return out


def _download(url: str, dest: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": rz.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception:
        return False
    if len(data) < 500:
        return False
    with open(dest, "wb") as fh:
        fh.write(data)
    return True


def load_tm(out_dir: str) -> dict:
    path = os.path.join(out_dir, "tm_squads.json")
    if not os.path.exists(path):
        raise SystemExit("run the `tm` stage first (tm_squads.json is missing)")
    return json.load(open(path, encoding="utf-8"))


def _tm_token_set(tm: dict) -> list[tuple[str, ...]]:
    return [norm_tokens(p["name"]) for sq in tm.values() for p in sq["players"]]


OVR_MIN = 70      # scan-pos stops below this; the user's wanted range is 70-114
OVR_MAX = 114     # the league's card ceiling: a card above it can never be traded
EXCLUDED_PROGRAM_SUFFIXES = ("_ICON", "_HERO")    # icons and heroes are not in the league


def is_wanted(row: dict, ovr_max: int | None = OVR_MAX, keep_icons: bool = False) -> bool:
    """False for a card above the OVR ceiling and, unless `keep_icons`, for an icon or a hero."""
    if ovr_max and (row.get("ovr") or 0) > ovr_max:
        return False
    if keep_icons:
        return True
    # the program tag is missing from some portrait URLs (retired legends): the card background names the type too
    program = (row.get("program") or "").upper()
    bg_tokens = (row.get("bg") or "").upper().split("_")
    return not (program.endswith(EXCLUDED_PROGRAM_SUFFIXES) or {"ICON", "HERO"} & set(bg_tokens))


def _first_page_within_cap(cap: int, delay: float) -> int:
    """The list is sorted by OVR descending: bisect for the first page that has a card <= cap."""
    lo, hi = 1, LAST_PAGE
    while lo < hi:
        mid = (lo + hi) // 2
        rows = parse_card_rows(rz.fetch(rz.page_url(mid, None)))
        time.sleep(delay)
        if rows and min(r["ovr"] or 0 for r in rows) <= cap:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _fetch_retry(url: str, tries: int = 4) -> str:
    """`rz.fetch` that survives a dropped connection; an HTTP error is final and is raised at once."""
    for attempt in range(tries):
        try:
            return rz.fetch(url)
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def stage_scan(args) -> int:
    tm_tokens = _tm_token_set(load_tm(args.out))
    rows_path = os.path.join(args.out, "renderz_rows.jsonl")
    raw_dir = os.path.join(args.out, "portraits_raw")
    os.makedirs(raw_dir, exist_ok=True)
    done = rz._done_pages(rows_path)
    first, _, last = args.pages.partition("-")
    if args.pages == f"1-{LAST_PAGE}" and args.ovr_max:
        first = str(_first_page_within_cap(args.ovr_max, args.delay))
        print(f"cards above OVR {args.ovr_max} end before page {first}; scanning from there", flush=True)
    pages = [n for n in range(int(first), int(last or first) + 1) if n not in done]
    total = ports = 0
    with open(rows_path, "a", encoding="utf-8") as out:
        for n in pages:
            try:
                rows = parse_card_rows(_fetch_retry(rz.page_url(n, None)))
            except urllib.error.HTTPError as exc:
                print(f"page {n}: HTTP {exc.code}, stopping", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"page {n}: {exc}, stopping", file=sys.stderr)
                return 1
            if not rows:
                print(f"page {n}: no cards, end of list")
                break
            got = _store_rows(rows, n, out, raw_dir, tm_tokens, args)
            total += len(rows)
            ports += got
            print(f"page {n}: {len(rows)} cards, {got} portraits", flush=True)
            time.sleep(args.delay)
    print(f"scan: {total} new cards, {ports} portraits -> {args.out}")
    return 0


def _store_rows(rows, page_key, out, raw_dir, tm_tokens, args) -> int:
    """Write the wanted rows of one list page and download their portraits; returns portraits got."""
    got = 0
    for row in rows:
        if not is_wanted(row, args.ovr_max, args.keep_icons):
            continue                 # above the ceiling, icon or hero: not stored, no portrait
        row["page"] = page_key
        dest = os.path.join(raw_dir, f"{row['ea_id']}.png") if row["ea_id"] else None
        if (dest and row["portrait_url"] and not os.path.exists(dest)
                and any(tokens_match(norm_tokens(row["name"]), t) for t in tm_tokens)):
            got += _download(row["portrait_url"], dest)
        row.pop("portrait_url")      # signed, expires: never stored
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    out.flush()
    return got


POSITIONS = ("goalkeeper", "defender", "midfielder", "winger", "striker")
POS_PAGE_LIMIT = 4096        # a position list is not clamped at LAST_PAGE; the server answers 500 past its end


def _pos_url(pos: str, page: int) -> str:
    return f"{rz.BASE}/players/position/{pos}?page={page}"


def _first_pos_page_within_cap(pos: str, cap: int, delay: float) -> int:
    """Bisect a position list (OVR descending) for the first page that has a card <= cap."""
    lo, hi = 1, POS_PAGE_LIMIT
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            rows = parse_card_rows(_fetch_retry(_pos_url(pos, mid)))
        except urllib.error.HTTPError:
            rows = None                      # past the end of the list
        time.sleep(delay)
        if not rows or min(r["ovr"] or 0 for r in rows) <= cap:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _list_url(key: int) -> str:
    """URL of the list page a stored ``page`` key came from (see `stage_scan_pos` for the encoding)."""
    if key < 100000:
        return rz.page_url(key, None)
    return _pos_url(POSITIONS[key // 100000 - 1], key % 100000)


def stage_scan_pos(args) -> int:
    """The position lists ``/players/position/<pos>?page=K`` are sorted by OVR too but are not clamped
    at page 416 like the main list, so they reach the cards below OVR 107.

    Each list is scanned from its first page at or below ``--ovr-max`` down to the first page whose best
    card is under ``--ovr-min``. ``page`` is stored as ``(position index + 1) * 100000 + K`` so it never
    collides with the main list's page numbers (the resume key).
    """
    tm_tokens = _tm_token_set(load_tm(args.out))
    rows_path = os.path.join(args.out, "renderz_rows.jsonl")
    raw_dir = os.path.join(args.out, "portraits_raw")
    os.makedirs(raw_dir, exist_ok=True)
    done = rz._done_pages(rows_path)
    total = ports = 0
    with open(rows_path, "a", encoding="utf-8") as out:
        for idx, pos in enumerate(POSITIONS):
            first = _first_pos_page_within_cap(pos, args.ovr_max, args.delay) if args.ovr_max else 1
            print(f"{pos}: cards <= OVR {args.ovr_max} start at page {first}", flush=True)
            for k in range(first, POS_PAGE_LIMIT + 1):
                key = (idx + 1) * 100000 + k
                if key in done:
                    continue
                try:
                    rows = parse_card_rows(_fetch_retry(_pos_url(pos, k)))
                except urllib.error.HTTPError as exc:
                    print(f"{pos} page {k}: HTTP {exc.code}, end of the list")
                    break
                if not rows or max(r["ovr"] or 0 for r in rows) < args.ovr_min:
                    print(f"{pos} page {k}: below OVR {args.ovr_min}, done")
                    break
                rows = [r for r in rows if (r["ovr"] or 0) >= args.ovr_min]
                got = _store_rows(rows, key, out, raw_dir, tm_tokens, args)
                total += len(rows)
                ports += got
                print(f"{pos} page {k}: {len(rows)} cards, {got} portraits", flush=True)
                time.sleep(args.delay)
    print(f"scan-pos: {total} new cards, {ports} portraits -> {args.out}")
    return 0


# ---------------------------------------------------------------- stage match

MIN_CLUB_VOTES = 3
CLUB_SHARE = 0.6


def _read_rows(out_dir: str, ovr_max: int | None = None, keep_icons: bool = False) -> list[dict]:
    path = os.path.join(out_dir, "renderz_rows.jsonl")
    if not os.path.exists(path):
        raise SystemExit("run the `scan` stage first (renderz_rows.jsonl is missing)")
    seen, rows = set(), []
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["id"] not in seen and is_wanted(r, ovr_max, keep_icons):
            seen.add(r["id"])
            rows.append(r)
    return rows


_CLUB_NOISE = {"fc", "cf", "sc", "ac", "afc", "sl", "ssc", "as", "rc", "cd", "ud", "rcd", "sv", "vfb", "vfl",
               "tsg", "fk", "sk", "club", "de", "the", "1", "04", "05", "1899", "1909", "1846"}


def _club_tokens(name: str) -> set[str]:
    return {t for t in norm_tokens(name) if t not in _CLUB_NOISE and len(t) > 1}


def _resolve_by_detail(c: set[int], cards: list[dict], squad: list, tm: dict, names: dict) -> int | None:
    """An ambiguous group, settled by the full name (and team) read from the cards' detail pages.

    Every card of the group whose page was read must point at the same Transfermarkt player:
    the full name decides (equal token sets first, then one name covering the other), the team
    breaks a tie between namesakes («Nico González» at two clubs). Anything else stays ambiguous.
    """
    picks = set()
    for card in cards:
        info = names.get(card["id"])
        if not info or not info.get("full_name"):
            continue
        ft = norm_tokens(info["full_name"])
        hits = [i for i in c if set(ft) == set(squad[i][2])]
        if not hits:
            hits = [i for i in c if tokens_match(ft, squad[i][2]) or tokens_match(squad[i][2], ft)]
        if len(hits) > 1 and info.get("team"):
            team = _club_tokens(info["team"])
            hits = [i for i in hits if team & _club_tokens(tm[squad[i][0]].get("tm_name") or "")] or hits
        if len(hits) != 1:
            return None
        picks.add(hits[0])
    return next(iter(picks)) if len(picks) == 1 else None


def run_match(tm: dict, rows: list[dict], names: dict | None = None) -> dict:
    """Pure: decide which Renderz players are which Transfermarkt players.

    ``names`` — ``{renderz id: {"full_name", "team"}}`` from the ``resolve`` stage: list cards
    carry the surname only, so namesakes are told apart by the card's detail page.

    Returns ``{"players": [...], "ambiguous": [...], "club_map": {...}}``.
    """
    names = names or {}
    squad = [(club, p["name"], norm_tokens(p["name"])) for club, sq in tm.items() for p in sq["players"]]
    by_ea: dict[int, list[dict]] = defaultdict(list)
    for r in rows:      # a card without an action shot has no EA id: it stands alone (negative key)
        by_ea[r.get("ea_id") or -r["id"]].append(r)

    # candidates per EA player: TM players some card name of the cluster matches
    cands: dict[int, set[int]] = {}
    exact: dict[int, set[int]] = {}
    token_cache: dict[tuple[str, ...], tuple[set[int], set[int]]] = {}
    for ea, cards in by_ea.items():
        c, e = set(), set()
        for card in cards:
            ct = norm_tokens(card["name"])
            if ct not in token_cache:
                c_matches = set()
                e_matches = set()
                for i, (_club, _name, pt) in enumerate(squad):
                    if tokens_match(ct, pt):
                        c_matches.add(i)
                        if set(ct) == set(pt):
                            e_matches.add(i)
                token_cache[ct] = (c_matches, e_matches)
            cm, em = token_cache[ct]
            c.update(cm)
            e.update(em)
        if c:
            cands[ea], exact[ea] = c, e

    # learn which Renderz club image is which of our clubs from unambiguous full-name matches
    votes: dict[int, Counter] = defaultdict(Counter)
    for ea, e in exact.items():
        if len(e) == 1 and len(cands[ea]) == 1:
            club = squad[next(iter(e))][0]
            for card in by_ea[ea]:
                if card.get("club_img"):
                    votes[card["club_img"]][club] += 1
    club_map = {}
    for img, cnt in votes.items():
        club, n = cnt.most_common(1)[0]
        if n >= MIN_CLUB_VOTES and n / sum(cnt.values()) >= CLUB_SHARE:
            club_map[img] = club

    ambiguous = []
    by_pick: dict[int, dict] = {}      # one entry per TM player: cards without an EA id stand alone above
    for ea, c in cands.items():
        pick = None
        if len(c) == 1:
            pick = next(iter(c))
        else:
            clubs_of_cards = {club_map[x["club_img"]] for x in by_ea[ea] if x.get("club_img") in club_map}
            narrowed = [i for i in c if squad[i][0] in clubs_of_cards]
            if len(narrowed) == 1:
                pick = narrowed[0]
            elif len(exact[ea]) == 1:
                pick = next(iter(exact[ea]))
            else:
                pick = _resolve_by_detail(c, by_ea[ea], squad, tm, names)
        card_names = sorted({card["name"] for card in by_ea[ea]})
        if pick is None:
            ambiguous.append({"ea_id": ea, "card_names": card_names,
                              "candidates": [f"{squad[i][1]} ({squad[i][0]})" for i in sorted(c)]})
            continue
        acc = by_pick.setdefault(pick, {"ea_id": 0, "cards": {}})
        acc["ea_id"] = acc["ea_id"] or (ea if ea > 0 else 0)
        for card in by_ea[ea]:
            acc["cards"][card["id"]] = card

    players = []
    for pick, acc in by_pick.items():
        club, tm_name, _ = squad[pick]
        # one card per (program, ovr, position); the tradable copy wins, a lone untradable one is kept.
        # Most cards carry no program tag: their background image names the program instead.
        best: dict[tuple, dict] = {}
        for card in acc["cards"].values():
            key = (card.get("program") or card.get("bg") or f"id{card['id']}", card["ovr"], card["position"])
            cur = best.get(key)
            if cur is None or (card["tradable"] and not cur["tradable"]):
                best[key] = card
        chosen = {b["id"] for b in best.values()}
        versions = [{"renderz_id": x["id"], "slug": x["slug"], "card_name": x["name"], "ovr": x["ovr"],
                     "position": x["position"], "program": x.get("program"), "tradable": x["tradable"],
                     "club_img": x.get("club_img"), "page": x["page"], "selected": x["id"] in chosen}
                    for x in sorted(acc["cards"].values(), key=lambda x: (-(x["ovr"] or 0), x["id"]))]
        players.append({"player": tm_name, "club": club, "ea_id": acc["ea_id"], "versions": versions})
    players.sort(key=lambda p: (p["club"], p["player"]))
    return {"players": players, "ambiguous": ambiguous,
            "club_map": {str(k): v for k, v in sorted(club_map.items())}}


def portrait_slug(player_name: str) -> str:
    from services.graphics.player_photos import _slugify
    return _slugify(player_name)


# ---------------------------------------------------------------- stage resolve


def parse_card_detail(page_html: str) -> dict:
    """Read the full name and club from a player card's detail page.

    Full name is extracted from Schema.org Person JSON-LD, falling back to <title> or <h1>.
    Club is extracted from the uppercase TEAM block in HTML.
    """
    full_name = None
    team = None
    for block in re.findall(r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', page_html, re.S):
        try:
            data = json.loads(block)
            items = data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Person":
                    full_name = html.unescape(item.get("name") or "").strip() or None
                    break
        except Exception:
            pass
        if full_name:
            break
    if not full_name:
        m = re.search(r'<title>\s*(.*?)\s*—\s*\d+\s*OVR', page_html)
        if m:
            full_name = html.unescape(m.group(1)).strip()
    m = re.search(r'TEAM</span>\s*(?:<!--.*?-->)*\s*<[a-z0-9]+[^>]*>([^<]+)</[a-z0-9]+>', page_html, re.I)
    if m:
        team = html.unescape(m.group(1)).strip()
    return {"full_name": full_name, "team": team}


def stage_resolve(args) -> int:
    """Fetch detail pages for cards in ambiguous surname clusters to disambiguate players.

    Reads `renderz_rows.jsonl` and Transfermarkt squads, runs matching to find ambiguous groups,
    and fetches one card detail page per ambiguous cluster (saving full name and club to
    `card_details.json`). Resumable: already fetched cards are skipped.
    """
    tm = load_tm(args.out)
    rows = _read_rows(args.out, args.ovr_max, args.keep_icons)
    details_path = os.path.join(args.out, "card_details.json")
    details = {}
    if os.path.exists(details_path):
        with open(details_path, encoding="utf-8") as fh:
            try:
                details = json.load(fh)
            except Exception:
                details = {}
    names = {int(k): v for k, v in details.items()}

    res = run_match(tm, rows, names=names)
    amb = res["ambiguous"]
    if not amb:
        print("resolve: no ambiguous card groups, nothing to fetch")
        return 0

    by_ea: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_ea[r.get("ea_id") or -r["id"]].append(r)

    # Pick candidate cards to fetch: one card per ambiguous cluster not yet fetched
    todo = []
    for a in amb:
        ea = a["ea_id"]
        candidates = sorted(by_ea[ea], key=lambda x: (x.get("club_img") is not None, x.get("ovr") or 0), reverse=True)
        for c in candidates:
            if c["id"] not in names:
                todo.append(c)
                break

    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        todo = todo[:limit]

    print(f"resolve: {len(amb)} ambiguous groups, {len(todo)} cards to fetch ({len(details)} already in {details_path})", flush=True)
    if not todo:
        return 0

    fetched = 0
    for idx, card in enumerate(todo, 1):
        cid = card["id"]
        slug = card.get("slug")
        url = f"{rz.BASE}/player/{cid}-{slug}" if slug else f"{rz.BASE}/player/{cid}"
        try:
            page_html = _fetch_retry(url)
            info = parse_card_detail(page_html)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                print(f"[{idx}/{len(todo)}] card {cid}: HTTP 429 rate limit, sleeping 30s...", file=sys.stderr)
                time.sleep(30)
                try:
                    page_html = _fetch_retry(url)
                    info = parse_card_detail(page_html)
                except Exception as exc2:
                    print(f"[{idx}/{len(todo)}] card {cid}: retry failed ({exc2}), skipped", file=sys.stderr)
                    info = {"full_name": None, "team": None}
            else:
                print(f"[{idx}/{len(todo)}] card {cid}: HTTP {exc.code}, skipped", file=sys.stderr)
                info = {"full_name": None, "team": None}
        except Exception as exc:
            print(f"[{idx}/{len(todo)}] card {cid}: {exc}, skipped", file=sys.stderr)
            info = {"full_name": None, "team": None}

        details[str(cid)] = info
        names[cid] = info
        fetched += 1
        fn = info.get("full_name") or "-"
        tm_name = info.get("team") or "-"
        print(f"[{idx}/{len(todo)}] card {cid} ({card['name']}): full=«{fn}», team=«{tm_name}»", flush=True)

        if fetched % 10 == 0 or idx == len(todo):
            tmp_file = details_path + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(details, fh, ensure_ascii=False, indent=1)
            os.replace(tmp_file, details_path)

        if idx < len(todo):
            time.sleep(args.delay)

    tmp_file = details_path + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as fh:
        json.dump(details, fh, ensure_ascii=False, indent=1)
    os.replace(tmp_file, details_path)

    res_after = run_match(tm, rows, names=names)
    resolved_count = len(amb) - len(res_after["ambiguous"])
    print(f"resolve: fetched {fetched} cards -> {details_path}; resolved {resolved_count} of {len(amb)} ambiguous groups (remaining ambiguous: {len(res_after['ambiguous'])})")
    return 0


def stage_match(args) -> int:
    tm = load_tm(args.out)
    details_path = os.path.join(args.out, "card_details.json")
    names = {}
    if os.path.exists(details_path):
        with open(details_path, encoding="utf-8") as fh:
            try:
                names = {int(k): v for k, v in json.load(fh).items()}
            except Exception:
                names = {}
    res = run_match(tm, _read_rows(args.out, args.ovr_max, args.keep_icons), names=names)
    with open(os.path.join(args.out, "ovr_db.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    selected = [{"renderz_id": v["renderz_id"], "player": p["player"], "club": p["club"],
                 "ovr": v["ovr"], "tradable": v["tradable"], "page": v["page"]}
                for p in res["players"] for v in p["versions"] if v["selected"]]
    with open(os.path.join(args.out, "selected_cards.json"), "w", encoding="utf-8") as fh:
        json.dump(selected, fh, ensure_ascii=False, indent=1)

    raw_dir = os.path.join(args.out, "portraits_raw")
    dest_dir = os.path.join(args.out, "portraits")
    os.makedirs(dest_dir, exist_ok=True)
    copied = 0
    for p in res["players"]:
        src = os.path.join(raw_dir, f"{p['ea_id']}.png")
        if p["ea_id"] and os.path.exists(src):
            dest = os.path.join(dest_dir, portrait_slug(p["player"]) + ".png")
            if not os.path.exists(dest):
                with open(src, "rb") as a, open(dest, "wb") as b:
                    b.write(a.read())
                copied += 1
    squad_total = sum(len(sq["players"]) for sq in tm.values())
    per_club = Counter(p["club"] for p in res["players"])
    print(f"match: {len(res['players'])} of {squad_total} Transfermarkt players have a Renderz card, "
          f"{len(selected)} cards selected, {copied} portraits, {len(res['ambiguous'])} ambiguous, "
          f"{len(res['club_map'])} club images learned")
    for club in CLUBS:
        if per_club[club] == 0:
            print(f"  no cards at all for {club}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- stage cards


def stage_cards(args) -> int:
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

    path = os.path.join(args.out, "selected_cards.json")
    if not os.path.exists(path):
        raise SystemExit("run the `match` stage first (selected_cards.json is missing)")
    cards_dir = os.path.join(args.out, "cards")
    os.makedirs(cards_dir, exist_ok=True)
    wanted: dict[int, set[int]] = defaultdict(set)
    for c in json.load(open(path, encoding="utf-8")):
        if not os.path.exists(os.path.join(cards_dir, f"{c['renderz_id']}.png")):
            wanted[c["page"]].add(c["renderz_id"])
    if not wanted:
        print("cards: all selected cards are already saved")
        return 0
    saved = 0
    with sync_playwright() as pw:
        browser = rz._launch(pw)
        page = browser.new_page(viewport={"width": 1280, "height": 1400}, device_scale_factor=args.scale)
        skipped = []
        for n in sorted(wanted):
            try:
                page.goto(_list_url(n), wait_until="networkidle", timeout=60000)
            except PWTimeout:
                try:                                     # one retry: a slow page, not a dead one
                    page.goto(_list_url(n), wait_until="networkidle", timeout=60000)
                except PWTimeout:
                    print(f"  page {n}: did not load, skipped (rerun `cards` to retry)", file=sys.stderr)
                    skipped.append(n)
                    continue
            page.wait_for_timeout(1000)
            page.evaluate(rz.HIDE_OVERLAYS_JS)
            try:
                rz._settle(page)
            except PWTimeout:                            # a broken image never loads; `_shoot` drops blank cards
                print(f"  page {n}: some images did not load", file=sys.stderr)
            cards = page.locator("[data-player-card]:visible")
            for i in range(cards.count()):
                card = cards.nth(i)
                href = card.evaluate("e => e.closest('a')?.getAttribute('href') || ''")
                m = rz.HREF.search(f'href="{href}"')
                if not m or int(m.group(1)) not in wanted[n]:
                    continue
                if rz._shoot(page, card, os.path.join(cards_dir, f"{m.group(1)}.png")):
                    saved += 1
                else:
                    print(f"  card {m.group(1)}: stayed blank, skipped", file=sys.stderr)
            print(f"page {n}: {len(wanted[n])} wanted, {saved} saved so far", flush=True)
            time.sleep(args.delay)
        browser.close()
    print(f"cards: {saved} saved -> {cards_dir}" + (f"; {len(skipped)} pages skipped" if skipped else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["tm", "scan", "scan-pos", "resolve", "match", "cards", "all"])
    ap.add_argument("--out", default="renderz_sync", help="output folder (default renderz_sync/)")
    ap.add_argument("--as-of", type=dt.date.fromisoformat, default=AS_OF,
                    help="squads as of this date, YYYY-MM-DD (default 2026-09-17)")
    ap.add_argument("--club", action="append", help="tm stage: only this club (Russian name), repeatable")
    ap.add_argument("--pages", default=f"1-{LAST_PAGE}", help=f"scan stage: page range (default 1-{LAST_PAGE})")
    ap.add_argument("--ovr-max", type=int, default=OVR_MAX,
                    help=f"ignore cards above this OVR (default {OVR_MAX}; 0 = no ceiling)")
    ap.add_argument("--ovr-min", type=int, default=OVR_MIN,
                    help=f"scan-pos stage: stop a position list below this OVR (default {OVR_MIN})")
    ap.add_argument("--keep-icons", action="store_true", help="keep icon and hero cards (dropped by default)")
    ap.add_argument("--limit", type=int, default=0, help="resolve stage: stop after N cards (default 0 = all)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between Renderz pages (default 2)")
    ap.add_argument("--tm-delay", type=float, default=2.0, help="seconds between Transfermarkt pages (default 2)")
    ap.add_argument("--scale", type=int, default=3, help="cards stage: screenshot scale (default 3)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    stages = {"tm": stage_tm, "scan": stage_scan, "scan-pos": stage_scan_pos,
              "resolve": stage_resolve, "match": stage_match, "cards": stage_cards}
    for name in (["tm", "scan", "scan-pos", "resolve", "match", "cards"] if args.stage == "all" else [args.stage]):
        print(f"== {name} ==", flush=True)
        code = stages[name](args)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
