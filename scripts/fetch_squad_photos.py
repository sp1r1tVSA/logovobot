"""
scripts/fetch_squad_photos.py

Перекачивает фотографии игроков из `squad_players`, опознавая каждого игрока
**внутри состава его клуба**, а не поиском по имени во всём мире.

Раньше фетчер бота искал игрока у провайдеров глобально по имени: состав
читается OCR из русифицированного FC, там одни фамилии заглавными («MENDY»,
«MARTÍNEZ», «DAVID»), и такой запрос регулярно приводил чужого футболиста —
отсюда и неверные лица на карточках. Состав же у участников реальный, поэтому
клуб здесь — не подсказка, а ограничение: сначала берём ростер клуба целиком и
сопоставляем фамилию только с ним. «MENDY» в Аль-Ахли — это Édouard Mendy и
никто другой; «MARTÍNEZ» в Интере с позицией ST — Lautaro, а не вратарь Josep.

Само опознание живёт в `services/graphics/player_identity.py`, и тем же кодом
`player_photos.fetch_and_cache` теперь качает фото новичкам, когда клуб известен.
Скрипт нужен для массовой перезаливки: он не доверяет уже лежащим файлам (их
клал прежний фетчер), ведёт манифест и собирает контактные листы.

Конвейер:

1. **Личность** — ростер клуба из FotMob (`/api/data/teams?id=`): полное имя,
   дата рождения, позиция, номер, id. Покрывает и Саудовскую лигу, и MLS,
   и тайскую Бурираму.
2. **Сопоставление** — тиры EXACT → все токены → фамилия → токен → fuzzy,
   строго внутри ростера. Ничья разводится позицией из БД, затем — известностью
   (рейтинг/трансферная стоимость), и только с большим отрывом. Неразведённая
   ничья — это отказ: пустая карточка восстановима, чужое лицо нет.
3. **Фото** — каскад по качеству, где **каждый источник проверяется на совпадение
   личности**: TheSportsDB (500px вырезка, сверка по клубу или дате рождения) →
   SoFIFA (360px рендер EA FC, сверка по клубу в строке поиска) → FotMob
   (192px, личность гарантирована id — крайний случай).

Файлы пишутся ровно туда, откуда их читает бот — `player_photos.get_cached_photo_path`
(`assets/players/<игрок>_<клуб>.png`), поэтому после прогона ничего доустанавливать
не нужно. Рядом кладётся `_photo_manifest.json`: что за игрок опознан, каким
источником, с какой уверенностью — по нему видно, что проверять руками.

    # посмотреть план, ничего не скачивая
    python scripts/fetch_squad_photos.py --db ../server_league.db --dry-run

    # прогон по одному клубу с контактным листом для глазной проверки
    python scripts/fetch_squad_photos.py --db ../server_league.db --club Арсенал --contact-sheet

    # полный прогон на сервере
    venv/bin/python3 scripts/fetch_squad_photos.py --contact-sheet --clear-media-cache
"""

import os
import sys
import json
import time
import sqlite3
import logging
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# Скрипт запускают и на сервере, где системный python — без Pillow: там бот
# живёт в venv. Перезапускаемся в нём сами, как делает refresh_all_player_cards.
for _venv_py in (
    os.path.join(BASE_DIR, "venv", "bin", "python3"),
    os.path.join(BASE_DIR, "venv", "bin", "python"),
    os.path.join(BASE_DIR, ".venv", "bin", "python3"),
    os.path.join(BASE_DIR, ".venv", "bin", "python"),
    os.path.join(BASE_DIR, "venv", "Scripts", "python.exe"),
):
    if os.path.isfile(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            os.execv(_venv_py, [_venv_py] + sys.argv)

try:
    from PIL import Image, ImageDraw
except ImportError:
    print(
        "\n❌ Pillow не найден в текущем интерпретаторе.\n"
        "   Запустите через venv бота:  venv/bin/python3 scripts/fetch_squad_photos.py\n"
    )
    sys.exit(1)

from services.graphics import player_photos

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("squad_photos")

MANIFEST_PATH = os.path.join(player_photos.PHOTOS_DIR, "_photo_manifest.json")
CONTACT_SHEET_DIR = os.path.join(BASE_DIR, "assets", "contact_sheets")

# Опознание и каскад источников живут в `services/graphics/player_identity.py` —
# тем же кодом бот подтягивает фото новичкам при загрузке состава. Имена
# реэкспортируются здесь, чтобы скрипт и его тесты не зависели от переезда.
from services.graphics.player_identity import (  # noqa: E402,F401
    CLUB_FOTMOB_IDS,
    PLAYER_NAME_OVERRIDES,
    PINNED_PLAYERS,
    REQUEST_PAUSE,
    _norm,
    match_in_roster,
    search_player_globally,
    fetch_club_roster,
    identify_in_roster,
    download_photo,
)


# --------------------------------------------------------------------------
# Данные и отчёты
# --------------------------------------------------------------------------

def load_squads(db_path: str, only_club: str | None = None) -> dict[str, list[tuple[str, str]]]:
    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(BASE_DIR, db_path))
    if not os.path.exists(db_path):
        logger.error(f"База не найдена: {db_path}")
        return {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT team_name, player_name, position FROM squad_players "
            "WHERE player_name IS NOT NULL AND player_name != '' ORDER BY team_name, id"
        ).fetchall()
    finally:
        conn.close()

    squads: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if only_club and row["team_name"].strip().lower() != only_club.strip().lower():
            continue
        squads.setdefault(row["team_name"], []).append((row["player_name"], row["position"]))
    return squads


def load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(manifest: dict) -> None:
    os.makedirs(player_photos.PHOTOS_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)


def build_contact_sheet(club_ru: str, entries: list[dict]) -> str | None:
    """
    Лист с лицами и подписями для глазной проверки: одного взгляда хватает,
    чтобы увидеть чужого футболиста в составе.
    """
    entries = [e for e in entries if e.get("path") and os.path.exists(e["path"])]
    if not entries:
        return None

    # Шрифт берём тот же, что и карточки бота: у шрифта PIL по умолчанию нет
    # кириллицы, и названия клубов вышли бы квадратами.
    from services.graphics.table_generator import load_font
    title_font, name_font, meta_font = load_font(16, bold=True), load_font(13), load_font(11)

    cell, pad, caption_h, columns = 180, 10, 40, 6
    rows = (len(entries) + columns - 1) // columns
    width = columns * (cell + pad) + pad
    height = rows * (cell + caption_h + pad) + pad + 30
    sheet = Image.new("RGB", (width, height), (24, 26, 32))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, pad), f"{club_ru} — {len(entries)} фото", fill=(235, 235, 235), font=title_font)

    for index, entry in enumerate(entries):
        col, row = index % columns, index // columns
        x = pad + col * (cell + pad)
        y = 30 + pad + row * (cell + caption_h + pad)
        try:
            with Image.open(entry["path"]) as im:
                im = im.convert("RGBA")
                im.thumbnail((cell, cell))
                box = Image.new("RGBA", (cell, cell), (40, 43, 52, 255))
                box.paste(im, ((cell - im.width) // 2, (cell - im.height) // 2), im)
                sheet.paste(box.convert("RGB"), (x, y))
        except Exception:
            draw.rectangle([x, y, x + cell, y + cell], fill=(70, 40, 40))
        draw.text((x, y + cell + 2), (entry["db_name"] or "")[:26], fill=(255, 255, 255), font=name_font)
        draw.text((x, y + cell + 16), (entry.get("identity") or "")[:30], fill=(150, 200, 255), font=meta_font)
        draw.text((x, y + cell + 28), (entry.get("source") or "")[:30], fill=(140, 140, 140), font=meta_font)

    os.makedirs(CONTACT_SHEET_DIR, exist_ok=True)
    out_path = os.path.join(CONTACT_SHEET_DIR, f"{player_photos._slugify(club_ru)}.png")
    sheet.save(out_path)
    return out_path


def purge_unidentified(entries: list[dict], dry_run: bool) -> int:
    """
    Убирает старые файлы тех, кого опознать не удалось.

    Их фото клал прежний фетчер — глобальным поиском по фамилии, то есть ровно
    тем способом, который и приносил чужие лица. Раз личность не подтверждена,
    файл подозрителен: силуэт вместо фото честнее, чем незнакомый футболист.
    Удаляем и версию с клубом, и безклубную — вторая работает запасной у
    `get_photo_path`, иначе бы она и осталась на карточке.
    """
    removed = 0
    for entry in entries:
        if entry.get("status") != "unidentified":
            continue
        for path in (player_photos.get_cached_photo_path(entry["db_name"], entry["club"]),
                     player_photos.get_cached_photo_path(entry["db_name"], None)):
            if not os.path.isfile(path):
                continue
            if dry_run:
                logger.info(f"  [dry-run] удалил бы {os.path.basename(path)}")
            else:
                os.remove(path)
                logger.info(f"  удалён подозрительный файл: {os.path.basename(path)}")
            removed += 1
    return removed


def clear_telegram_media_cache(db_path: str, dry_run: bool) -> int:
    """Старые карточки лежат в Telegram по file_id — без сброса бот отдаст их же."""
    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(BASE_DIR, db_path))
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM telegram_media_cache").fetchone()[0]
        if dry_run:
            logger.info(f"  [dry-run] очистил бы telegram_media_cache: {count} записей")
            return count
        conn.execute("DELETE FROM telegram_media_cache")
        conn.commit()
        logger.info(f"  очищен telegram_media_cache: {count} записей")
        return count
    except Exception as e:
        logger.error(f"  не смог очистить telegram_media_cache: {e}")
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Основной прогон
# --------------------------------------------------------------------------

def process_club(club_ru: str, players: list[tuple[str, str]], manifest: dict,
                 force: bool, dry_run: bool) -> list[dict]:
    logger.info(f"=== {club_ru} ({len(players)} игроков) ===")
    tid, club_en, roster = fetch_club_roster(club_ru)
    if not roster:
        logger.error(f"  ростер не получен (id={tid}) — клуб пропущен")
        return [{"club": club_ru, "db_name": name, "status": "no_roster"} for name, _ in players]

    logger.info(f"  ростер FotMob: {club_en} (id={tid}), {len(roster)} игроков")
    entries: list[dict] = []

    for db_name, position in players:
        key = f"{club_ru}|{db_name}"
        target = player_photos.get_cached_photo_path(db_name, club_ru)
        previous = manifest.get(key) or {}

        # Старый кэш писал фетчер бота — там и лежат чужие лица, поэтому файл сам
        # по себе не повод пропустить игрока. Пропускаем только то, что этот
        # скрипт уже опознал и скачал.
        if not force and previous.get("status") == "ok" and os.path.exists(target):
            entries.append({**previous, "club": club_ru, "db_name": db_name, "path": target})
            continue

        identity, why = identify_in_roster(db_name, position, club_ru, club_en, roster)
        if not identity:
            logger.warning(f"  ✗ {db_name} ({position}): {why}")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "status": "unidentified", "reason": why})
            continue

        label = f"{db_name} ({position}) → {identity['name']}"
        if dry_run:
            logger.info(f"  [dry-run] {label} [{why}]")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "identity": identity["name"], "status": "planned", "reason": why})
            continue

        photo = download_photo(identity)
        if not photo:
            logger.warning(f"  ✗ {label}: фото не нашлось ни у одного источника")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "identity": identity["name"], "status": "no_photo", "reason": why})
            continue

        os.makedirs(player_photos.PHOTOS_DIR, exist_ok=True)
        with open(target, "wb") as f:
            f.write(photo["data"])

        entry = {
            "club": club_ru, "club_en": club_en, "db_name": db_name, "position": position,
            "identity": identity["name"], "fotmob_id": identity.get("id"),
            "dob": identity.get("dob"), "match": why,
            "source": photo["source"], "url": photo["url"],
            "size": f"{photo['width']}x{photo['height']}", "alpha": photo["alpha"],
            "status": "ok", "path": target,
        }
        manifest[f"{club_ru}|{db_name}"] = {k: v for k, v in entry.items() if k != "path"}
        entries.append(entry)
        logger.info(f"  ✓ {label} — {photo['source']} {photo['width']}x{photo['height']}"
                    f"{'' if photo['alpha'] else ' (без прозрачности)'}")
        time.sleep(REQUEST_PAUSE)

    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачивает фотографии игроков, опознавая их внутри состава клуба.")
    parser.add_argument("--db", default="league.db",
                        help="путь к SQLite-базе (по умолчанию league.db в корне проекта)")
    parser.add_argument("--club", help="прогнать один клуб, например --club Арсенал")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, кого и как опознали — ничего не скачивать")
    parser.add_argument("--force", action="store_true",
                        help="перекачать даже то, что уже записано в манифест")
    parser.add_argument("--contact-sheet", action="store_true",
                        help="собрать по клубу лист с лицами для глазной проверки")
    parser.add_argument("--purge-unidentified", action="store_true",
                        help="удалить старые фото тех, кого опознать не удалось — они от прежнего фетчера")
    parser.add_argument("--clear-media-cache", action="store_true",
                        help="очистить telegram_media_cache, чтобы бот перерисовал карточки")
    args = parser.parse_args()

    squads = load_squads(args.db, args.club)
    if not squads:
        logger.error("Игроков не найдено — проверьте --db и --club.")
        return

    total = sum(len(v) for v in squads.values())
    logger.info(f"Клубов: {len(squads)}, игроков: {total}"
                f"{' — DRY RUN, ничего не пишем' if args.dry_run else ''}")

    manifest = load_manifest()
    all_entries: list[dict] = []
    for club_ru, players in squads.items():
        entries = process_club(club_ru, players, manifest, args.force, args.dry_run)
        all_entries.extend(entries)
        if not args.dry_run:
            save_manifest(manifest)
            if args.contact_sheet:
                sheet = build_contact_sheet(club_ru, entries)
                if sheet:
                    logger.info(f"  контактный лист: {sheet}")

    ok = [e for e in all_entries if e.get("status") == "ok"]
    planned = [e for e in all_entries if e.get("status") == "planned"]
    unidentified = [e for e in all_entries if e.get("status") == "unidentified"]
    no_photo = [e for e in all_entries if e.get("status") == "no_photo"]
    low_res = [e for e in ok if e.get("source", "").startswith("FotMob")]

    logger.info("=" * 60)
    if args.dry_run:
        logger.info(f"• Опознано и готово к загрузке: {len(planned)}")
    else:
        logger.info(f"• Скачано/уже есть: {len(ok)}")
        by_source: dict[str, int] = {}
        for entry in ok:
            by_source[entry.get("source", "?")] = by_source.get(entry.get("source", "?"), 0) + 1
        for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
            logger.info(f"    {source}: {count}")
        if low_res:
            logger.info(f"• Низкое разрешение (192px, FotMob): {len(low_res)}")
    logger.info(f"• Не опознаны: {len(unidentified)}")
    for entry in unidentified:
        logger.info(f"    {entry['club']}: {entry['db_name']} — {entry.get('reason')}")
    if no_photo:
        logger.info(f"• Опознаны, но фото нет: {len(no_photo)}")
        for entry in no_photo:
            logger.info(f"    {entry['club']}: {entry['db_name']} → {entry.get('identity')}")

    if args.purge_unidentified and unidentified:
        logger.info(f"• Удаление старых фото у неопознанных: {purge_unidentified(all_entries, args.dry_run)} файлов")

    if args.clear_media_cache and not args.dry_run:
        clear_telegram_media_cache(args.db, args.dry_run)

    if not args.dry_run:
        logger.info(f"• Манифест: {MANIFEST_PATH}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
