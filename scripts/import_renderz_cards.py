#!/usr/bin/env python3
"""
scripts/import_renderz_cards.py

Загрузка результата `scripts/renderz_sync.py` (папка с `ovr_db.json`) в бота:

  * версии карточек и их OVR → таблица `transfer_player_cards` (снимок заменяется целиком);
    из неё форма заявки подставляет OVR игрока;
  * портреты `portraits/<slug>.png` → `assets/renderz_portraits/` — запасной портрет, когда в
    `assets/players/` нормального нет. Уже лежащие файлы не перезаписываются.

Dry-run по умолчанию — печатает, что будет сделано, и ничего не пишет:

    python scripts/import_renderz_cards.py --src C:/path/to/renderz_sync
    python scripts/import_renderz_cards.py --src C:/path/to/renderz_sync --apply

Карточки на сервер приезжают вместе с этим скриптом: запустите его там же, где живёт `league.db`
(и положите туда папку `renderz_sync`).
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEST_DIR = PROJECT_ROOT / "assets" / "renderz_portraits"


def load_cards(src: str) -> tuple[list[dict], list[tuple[str, str]]]:
    """Плоский список версий карточек и пары (файл-источник, имя-назначения) портретов."""
    with open(os.path.join(src, "ovr_db.json"), encoding="utf-8") as fh:
        db = json.load(fh)

    cards = []
    for p in db["players"]:
        for v in p["versions"]:
            cards.append({
                "renderz_id": v["renderz_id"], "player_name": p["player"], "club": p["club"],
                "ovr": v["ovr"], "position": v.get("position"), "program": v.get("program"),
                "tradable": bool(v["tradable"]), "selected": bool(v["selected"]),
            })

    portraits = []
    portraits_dir = os.path.join(src, "portraits")
    # 1. Обычная папка src/portraits/
    if os.path.isdir(portraits_dir):
        for f in os.listdir(portraits_dir):
            if f.endswith(".png"):
                portraits.append((os.path.join(portraits_dir, f), f))
    # 2. Файлы распакованы в src напрямую или с бэкслэшем в имени (архивы Windows на Linux)
    if not portraits and os.path.isdir(src):
        for f in os.listdir(src):
            if f.endswith(".png"):
                clean_name = f.replace("\\", "/").split("/")[-1]
                portraits.append((os.path.join(src, f), clean_name))
    # 3. Рекурсивный поиск во вложенных папках
    if not portraits and os.path.isdir(src):
        for root, dirs, files in os.walk(src):
            for f in files:
                if f.endswith(".png"):
                    clean_name = f.replace("\\", "/").split("/")[-1]
                    portraits.append((os.path.join(root, f), clean_name))

    portraits.sort(key=lambda x: x[1])
    return cards, portraits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="папка результата renderz_sync.py (с ovr_db.json)")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только план)")
    args = ap.parse_args()

    cards, portraits = load_cards(args.src)
    new_portraits = [(f, n) for f, n in portraits if not (DEST_DIR / n).exists()]
    players = len({c["player_name"] for c in cards})
    print(f"карточек {len(cards)} у {players} игроков, из них выбрано {sum(c['selected'] for c in cards)}")
    print(f"портретов {len(portraits)}, новых для {DEST_DIR}: {len(new_portraits)}")
    if not args.apply:
        print("dry-run: ничего не записано (добавьте --apply)")
        return 0

    from database import init_db
    from transfers import repo

    init_db()
    written = repo.replace_player_cards(cards)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    for file, name in new_portraits:
        shutil.copyfile(file, DEST_DIR / name)
    print(f"записано карточек: {written}, скопировано портретов: {len(new_portraits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
