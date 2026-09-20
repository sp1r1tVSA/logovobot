#!/usr/bin/env python3
"""
scripts/seed_my_club_demo.py

Генератор комплексных тестовых данных для проверки Logovo.bet Mini App:
1. Вкладка «Мой Клуб»:
   - Профиль клуба, баланс, варны дисциплины (1/4).
   - «Мои матчи»: входящее предложение времени + матч без времени.
   - «Состав клуба»: 15 игроков с позициями и бейджами лидеров.
   - «История игр»: завершённые матчи с формой [В] [Н] [В], очками и кликабельным протоколом со скриншотом.
2. Вкладка «Линия»:
   - Открытые туры 1 и 2 с флагом bets_open = 1.
   - Матчи между другими клубами (Бавария vs Интер, Арсенал vs Ливерпуль и др.) для проверки ставок.
   - Сгенерированные рынки ставок (1X2, тоталы, форы, обе забьют).
   - Фильтры по статусу: «⚡ Все», «🔥 Открытые», «⏰ Скоро» (Тур 2), «✅ Завершённые».
   - Поиск по названию клуба.
3. Вкладка «Турнир»:
   - Таблица дивизиона 1.
   - Список бомбардиров и ассистентов с реальными голами игроков разных клубов.

Использование:
  python scripts/seed_my_club_demo.py
  python scripts/seed_my_club_demo.py --clean       # Очистить старые демо-данные и сгенерировать новые
  python scripts/seed_my_club_demo.py --clean-only  # Только очистить базу от демо-данных (без генерации)
  python scripts/seed_my_club_demo.py --list-users  # Показать список пользователей в базе
  python scripts/seed_my_club_demo.py --user-id 1642770076 --team "Реал Мадрид"
"""

import sys
import os
import argparse
from pathlib import Path

# Setup project root import path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
import database
from services.betting_engine import generate_round_markets

DEMO_ROSTER = [
    ("Тибо Куртуа", "ВР"),
    ("Дани Карвахаль", "ПЗ"),
    ("Антонио Рюдигер", "ЦЗ"),
    ("Эдер Милитао", "ЦЗ"),
    ("Ферлан Менди", "ЛЗ"),
    ("Орельен Тчуамени", "ЦОП"),
    ("Федерико Вальверде", "ЦП"),
    ("Эдуардо Камавинга", "ЦП"),
    ("Лука Модрич", "ЦП"),
    ("Джуд Беллингем", "ЦАП"),
    ("Родриго", "ПВ"),
    ("Винисиус Жуниор", "ЛВ"),
    ("Килиан Мбаппе", "НАП"),
    ("Эндрик", "НАП"),
    ("Арда Гюлер", "ЦАП"),
]

DEMO_OPPONENTS = [
    {"team": "Барселона", "user_id": 990101, "username": "barca_coach"},
    {"team": "Манчестер Сити", "user_id": 990102, "username": "city_master"},
    {"team": "Бавария", "user_id": 990103, "username": "bayern_boss"},
    {"team": "Ливерпуль", "user_id": 990104, "username": "klopp_style"},
    {"team": "Арсенал", "user_id": 990105, "username": "arteta_ball"},
    {"team": "Интер", "user_id": 990106, "username": "inter_forza"},
]

OTHER_SQUADS = {
    "Барселона": [
        ("Марк-Андре тер Стеген", "ВР"),
        ("Жюль Кунде", "ПЗ"),
        ("Пау Кубарси", "ЦЗ"),
        ("Иньиго Мартинес", "ЦЗ"),
        ("Алехандро Бальде", "ЛЗ"),
        ("Педри", "ЦП"),
        ("Марк Касадо", "ЦОП"),
        ("Дани Ольмо", "ЦАП"),
        ("Ламин Ямаль", "ПВ"),
        ("Рафинья", "ЛВ"),
        ("Роберт Левандовски", "НАП"),
    ],
    "Бавария": [
        ("Мануэль Нойер", "ВР"),
        ("Дайо Упамекано", "ЦЗ"),
        ("Ким Мин Джэ", "ЦЗ"),
        ("Альфонсо Дэвис", "ЛЗ"),
        ("Йозуа Киммих", "ЦОП"),
        ("Александар Павлович", "ЦП"),
        ("Джамал Мусиала", "ЦАП"),
        ("Майкл Олисе", "ПВ"),
        ("Серж Гнабри", "ЛВ"),
        ("Гарри Кейн", "НАП"),
    ],
    "Ливерпуль": [
        ("Алиссон", "ВР"),
        ("Вирджил ван Дейк", "ЦЗ"),
        ("Ибраима Конате", "ЦЗ"),
        ("Трент Александер-Арнольд", "ПЗ"),
        ("Эндрю Робертсон", "ЛЗ"),
        ("Алексис Мак Аллистер", "ЦП"),
        ("Райан Гравенберх", "ЦОП"),
        ("Доминик Собослаи", "ЦАП"),
        ("Мохамед Салах", "ПВ"),
        ("Луис Диас", "ЛВ"),
        ("Дарвин Нуньес", "НАП"),
    ],
    "Манчестер Сити": [
        ("Эдерсон", "ВР"),
        ("Рубен Диаш", "ЦЗ"),
        ("Мануэль Аканджи", "ЦЗ"),
        ("Йошко Гвардиол", "ЛЗ"),
        ("Кайл Уокер", "ПЗ"),
        ("Родри", "ЦОП"),
        ("Кевин Де Брёйне", "ЦАП"),
        ("Бернарду Силва", "ЦП"),
        ("Фил Фоден", "ПВ"),
        ("Жереми Доку", "ЛВ"),
        ("Эрлинг Холанн", "НАП"),
    ],
    "Арсенал": [
        ("Давид Райя", "ВР"),
        ("Вильям Салиба", "ЦЗ"),
        ("Габриэл Магальяйнс", "ЦЗ"),
        ("Деклан Райс", "ЦОП"),
        ("Мартин Эдегор", "ЦАП"),
        ("Букайо Сака", "ПВ"),
        ("Габриэл Мартинелли", "ЛВ"),
        ("Кай Хаверц", "НАП"),
    ],
    "Интер": [
        ("Янн Зоммер", "ВР"),
        ("Алессандро Бастони", "ЦЗ"),
        ("Франческо Ачерби", "ЦЗ"),
        ("Николо Барелла", "ЦП"),
        ("Хакан Чалханоглу", "ЦОП"),
        ("Генрих Мхитарян", "ЦП"),
        ("Маркус Тюрам", "НАП"),
        ("Лаутаро Мартинес", "НАП"),
    ],
}

def make_demo_svg(t1: str, s1: int, s2: int, t2: str, tour: int, events_text: list[str]) -> str:
    """Генерирует аккуратную SVG-карточку протокола матча для предпросмотра."""
    events_svg = ""
    y = 260
    for ev in events_text:
        events_svg += f"<text x='60' y='{y}' fill='%2394a3b8' font-size='16' font-family='sans-serif'>{ev}</text>"
        y += 30

    return (
        "data:image/svg+xml;utf8,"
        f"<svg xmlns='http://www.w3.org/2000/svg' width='800' height='460' viewBox='0 0 800 460'>"
        f"<rect width='800' height='460' fill='%230f141c'/>"
        f"<rect x='15' y='15' width='770' height='430' rx='14' fill='%23151e2b' stroke='%23f5b027' stroke-width='2'/>"
        f"<text x='400' y='55' fill='%23f5b027' font-size='20' font-weight='bold' text-anchor='middle' font-family='sans-serif'>ПРОТОКОЛ МАТЧА · ТУР {tour}</text>"
        f"<text x='230' y='130' fill='%23ffffff' font-size='26' font-weight='bold' text-anchor='middle' font-family='sans-serif'>{t1}</text>"
        f"<rect x='340' y='90' width='120' height='55' rx='10' fill='%230b0e14' stroke='%23f5b027' stroke-width='1.5'/>"
        f"<text x='400' y='130' fill='%23f5b027' font-size='36' font-weight='900' text-anchor='middle' font-family='sans-serif'>{s1} : {s2}</text>"
        f"<text x='570' y='130' fill='%23ffffff' font-size='26' font-weight='bold' text-anchor='middle' font-family='sans-serif'>{t2}</text>"
        f"<line x1='40' y1='175' x2='760' y2='175' stroke='%23283548' stroke-width='1.5'/>"
        f"<text x='60' y='215' fill='%23f5b027' font-size='16' font-weight='bold' font-family='sans-serif'>⚡ КЛЮЧЕВЫЕ СОБЫТИЯ:</text>"
        f"{events_svg}"
        f"<rect x='250' y='400' width='300' height='30' rx='6' fill='%230b0e14' stroke='%2322c55e' stroke-width='1'/>"
        f"<text x='400' y='420' fill='%2322c55e' font-size='13' font-weight='bold' text-anchor='middle' font-family='sans-serif'>✓ ВЕРИФИЦИРОВАНО ИИ ТЕМШИК</text>"
        f"</svg>"
    )


def purge_demo_data(user_id: int | None = None, team_name: str = "Реал Мадрид", division_id: int = 1):
    """
    Полная очистка базы данных от всех тестовых/демо записей:
    - Составы и игроки демо-команд (Реал, Барселона, Ман Сити, Бавария, Ливерпуль, Арсенал, Интер)
    - Тестовые матчи и их события (голы/ассисты)
    - Рынки ставок туров 1 и 2
    - Виртуальные аккаунты соперников (990101..990106)
    - Сброс клуба и варнов у целевого пользователя
    - Сброс тестовых рейтингов Эло
    """
    print(f"\n🧹 Полная очистка тестовых данных Logovo.bet...")
    database.init_db()

    all_demo_teams = [team_name] + [o["team"] for o in DEMO_OPPONENTS]
    demo_uids = [o["user_id"] for o in DEMO_OPPONENTS]

    with database.transaction() as conn:
        cursor = conn.cursor()

        ph_teams = ",".join("?" for _ in all_demo_teams)
        ph_uids = ",".join("?" for _ in demo_uids)

        # 1. Удаление составов
        cursor.execute(f"DELETE FROM squad_players WHERE team_name IN ({ph_teams})", all_demo_teams)
        print(f"   ✓ Удалены составы демо-команд ({len(all_demo_teams)} клубов)")

        # 2. Удаление событий матчей
        cursor.execute(f"DELETE FROM match_events WHERE team_name IN ({ph_teams})", all_demo_teams)
        print("   ✓ Удалены события матчей (голы, ассисты)")

        # 3. Удаление ставок и транзакций по тестовым матчам и тестовым соперникам
        cursor.execute(f"DELETE FROM coin_transactions WHERE user_id IN ({ph_uids})", demo_uids)
        cursor.execute(f"DELETE FROM user_bets WHERE user_id IN ({ph_uids})", demo_uids)
        cursor.execute(
            f"""
            DELETE FROM bet_items 
            WHERE match_id IN (
                SELECT id FROM matches 
                WHERE player1_team IN ({ph_teams}) 
                   OR player2_team IN ({ph_teams})
                   OR player1_id IN ({ph_uids})
                   OR player2_id IN ({ph_uids})
            )
            """,
            all_demo_teams + all_demo_teams + demo_uids + demo_uids
        )

        # 4. Удаление рынков ставок туров 1 и 2 и демо-матчей
        cursor.execute(
            f"""
            DELETE FROM bet_markets 
            WHERE match_id IN (
                SELECT id FROM matches 
                WHERE player1_team IN ({ph_teams}) 
                   OR player2_team IN ({ph_teams})
                   OR player1_id IN ({ph_uids})
                   OR player2_id IN ({ph_uids})
            )
            OR team1_name IN ({ph_teams})
            OR team2_name IN ({ph_teams})
            OR tour IN (1, 2)
            """,
            all_demo_teams + all_demo_teams + demo_uids + demo_uids + all_demo_teams + all_demo_teams
        )
        print("   ✓ Удалены рынки ставок демо-матчей и туров 1/2")

        # 5. Удаление тестовых матчей
        cursor.execute(
            f"""
            DELETE FROM matches 
            WHERE player1_team IN ({ph_teams}) 
               OR player2_team IN ({ph_teams})
               OR player1_id IN ({ph_uids})
               OR player2_id IN ({ph_uids})
            """,
            all_demo_teams + all_demo_teams + demo_uids + demo_uids
        )
        print("   ✓ Удалены тестовые матчи")

        # 6. Удаление виртуальных соперников
        cursor.execute(f"DELETE FROM users WHERE telegram_id IN ({ph_uids})", demo_uids)
        cursor.execute(f"DELETE FROM user_wallets WHERE user_id IN ({ph_uids})", demo_uids)
        print(f"   ✓ Удалены {len(demo_uids)} аккаунтов тестовых соперников")

        # 7. Сброс клуба у пользователя
        if user_id:
            cursor.execute("UPDATE users SET team_name = NULL, warn_count = 0 WHERE telegram_id = ?", (user_id,))
            print(f"   ✓ Сброшен клуб и варны у пользователя ID {user_id}")
        cursor.execute("UPDATE users SET team_name = NULL, warn_count = 0 WHERE LOWER(team_name) = LOWER(?)", (team_name,))

        # 8. Сброс рейтингов Эло
        cursor.execute(f"DELETE FROM team_ratings WHERE LOWER(team_name) IN ({ph_teams})", [t.lower() for t in all_demo_teams])
        print("   ✓ Сброшены рейтинги Эло для демо-команд")

        # 9. Сброс ставок в турах и удаление пустых демо-туров
        cursor.execute("UPDATE rounds SET bets_open = 0 WHERE division_id = ? AND round_number IN (1, 2)", (division_id,))
        cursor.execute(
            """
            DELETE FROM rounds 
            WHERE division_id = ? AND round_number IN (1, 2) 
              AND deadline IN ('Сегодня, 23:59', '18.09.2026 21:00')
              AND NOT EXISTS (
                  SELECT 1 FROM matches WHERE matches.division_id = rounds.division_id AND matches.round_number = rounds.round_number
              )
            """,
            (division_id,)
        )
        print("   ✓ Сброшены статусы ставок в турах")

    print("\n✅ База данных успешно очищена от тестовых данных!")


def seed_cabinet_demo(user_id: int, team_name: str = "Реал Мадрид", division_id: int = 1, clean: bool = False):
    print(f"\n🚀 Запуск комплексной генерации демо-данных для Logovo.bet...")
    print(f"   👤 Telegram ID игрока: {user_id}")
    print(f"   🛡 Игровой клуб: {team_name}")
    print(f"   🏆 Дивизион ID: {division_id}")

    database.init_db()
    database.ensure_canonical_divisions()

    # 1. Очистка старых данных при необходимости
    if clean:
        purge_demo_data(user_id=user_id, team_name=team_name, division_id=division_id)

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 2. Убеждаемся в наличии активного сезона
        cursor.execute("SELECT id FROM seasons WHERE status = 'active' ORDER BY id DESC LIMIT 1")
        s_row = cursor.fetchone()
        if not s_row:
            cursor.execute("SELECT id FROM seasons ORDER BY id DESC LIMIT 1")
            s_row = cursor.fetchone()
            if s_row:
                season_id = s_row["id"]
                cursor.execute("UPDATE seasons SET status = 'active' WHERE id = ?", (season_id,))
            else:
                cursor.execute("INSERT INTO seasons (name, status, created_at) VALUES ('Сезон 1', 'active', datetime('now', '+3 hours'))")
                season_id = cursor.lastrowid
        else:
            season_id = s_row["id"]

        cursor.execute("SELECT id, name FROM divisions WHERE id = ?", (division_id,))
        div_row = cursor.fetchone()
        div_name = div_row["name"] if div_row else f"Дивизион {division_id}"

        # 3. Регистрация основного пользователя
        cursor.execute("SELECT username FROM users WHERE telegram_id = ?", (user_id,))
        existing_u = cursor.fetchone()
        username = existing_u["username"] if existing_u and existing_u["username"] else f"coach_{user_id}"

        # Освобождаем имя команды у других пользователей, если оно было занято
        cursor.execute("UPDATE users SET team_name = NULL WHERE LOWER(team_name) = LOWER(?) AND telegram_id != ?", (team_name, user_id))

        cursor.execute("""
            INSERT INTO users (telegram_id, username, team_name, division_id, warn_count, role, registered_at)
            VALUES (?, ?, ?, ?, 1, 'player', datetime('now', '+3 hours'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                team_name = excluded.team_name,
                division_id = excluded.division_id,
                warn_count = 1
        """, (user_id, username, team_name, division_id))

        # 4. Регистрация соперников
        for opp in DEMO_OPPONENTS:
            cursor.execute("UPDATE users SET team_name = NULL WHERE LOWER(team_name) = LOWER(?) AND telegram_id != ?", (opp["team"], opp["user_id"]))
            cursor.execute("""
                INSERT INTO users (telegram_id, username, team_name, division_id, warn_count, role, registered_at)
                VALUES (?, ?, ?, ?, 0, 'player', datetime('now', '+3 hours'))
                ON CONFLICT(telegram_id) DO UPDATE SET
                    team_name = excluded.team_name,
                    division_id = excluded.division_id
            """, (opp["user_id"], opp["username"], opp["team"], division_id))

        # 5. Создание туров с открытыми ставками (bets_open = 1)
        cursor.execute("""
            INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline, bets_open)
            VALUES (?, ?, 1, 1, 'Сегодня, 23:59', 1)
            ON CONFLICT(season_id, division_id, round_number) DO UPDATE SET
                is_open = 1, deadline = 'Сегодня, 23:59', bets_open = 1
        """, (season_id, division_id))

        cursor.execute("""
            INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline, bets_open)
            VALUES (?, ?, 2, 0, '18.09.2026 21:00', 1)
            ON CONFLICT(season_id, division_id, round_number) DO UPDATE SET
                is_open = 0, deadline = '18.09.2026 21:00', bets_open = 1
        """, (season_id, division_id))

        # 6. Составы команд (squad_players)
        cursor.execute("DELETE FROM squad_players WHERE LOWER(team_name) = LOWER(?)", (team_name,))
        for player_name, pos in DEMO_ROSTER:
            cursor.execute("""
                INSERT OR REPLACE INTO squad_players (team_name, player_name, position)
                VALUES (?, ?, ?)
            """, (team_name, player_name, pos))

        for opp_tname, roster in OTHER_SQUADS.items():
            cursor.execute("DELETE FROM squad_players WHERE LOWER(team_name) = LOWER(?)", (opp_tname,))
            for player_name, pos in roster:
                cursor.execute("""
                    INSERT OR REPLACE INTO squad_players (team_name, player_name, position)
                    VALUES (?, ?, ?)
                """, (opp_tname, player_name, pos))

        opp_barca = DEMO_OPPONENTS[0]
        opp_city = DEMO_OPPONENTS[1]
        opp_bayern = DEMO_OPPONENTS[2]
        opp_liv = DEMO_OPPONENTS[3]
        opp_arsenal = DEMO_OPPONENTS[4]
        opp_inter = DEMO_OPPONENTS[5]

        # 7. Активные несыгранные матчи клуба (Тур 1)
        # Матч 1: Реал Мадрид vs Арсенал (Соперник предложил время)
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, proposed_time, proposed_by, time_status
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 'pending', 'Сегодня, 21:30', ?, 'proposed')
        """, (season_id, division_id, user_id, opp_arsenal["user_id"], team_name, opp_arsenal["team"], opp_arsenal["user_id"]))

        # Матч 2: Манчестер Сити vs Реал Мадрид (Время ещё не предложено)
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, proposed_time, proposed_by, time_status
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 'pending', NULL, NULL, 'none')
        """, (season_id, division_id, opp_city["user_id"], user_id, opp_city["team"], team_name))

        # 8. Несыгранные матчи между ДРУГИМИ командами в Тур 1 (для проверки ставок и фильтров)
        # Бавария vs Интер
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, proposed_time, time_status
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 'pending', 'Завтра, 20:00', 'agreed')
        """, (season_id, division_id, opp_bayern["user_id"], opp_inter["user_id"], opp_bayern["team"], opp_inter["team"]))

        # Ливерпуль vs Барселона
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, proposed_time, time_status
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 'pending', 'Сегодня, 22:15', 'proposed')
        """, (season_id, division_id, opp_liv["user_id"], opp_barca["user_id"], opp_liv["team"], opp_barca["team"]))

        # 9. Матчи в Тур 2 («Ранняя линия» / «⏰ Скоро»)
        # Реал Мадрид vs Ливерпуль
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, time_status
            ) VALUES (?, ?, 2, 'league', ?, ?, ?, ?, 'pending', 'none')
        """, (season_id, division_id, user_id, opp_liv["user_id"], team_name, opp_liv["team"]))

        # Манчестер Сити vs Бавария
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, time_status
            ) VALUES (?, ?, 2, 'league', ?, ?, ?, ?, 'pending', 'none')
        """, (season_id, division_id, opp_city["user_id"], opp_bayern["user_id"], opp_city["team"], opp_bayern["team"]))

        # Интер vs Арсенал
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                status, time_status
            ) VALUES (?, ?, 2, 'league', ?, ?, ?, ?, 'pending', 'none')
        """, (season_id, division_id, opp_inter["user_id"], opp_arsenal["user_id"], opp_inter["team"], opp_arsenal["team"]))

        # 10. Сыгранные матчи клуба («История игр» + скриншоты протоколов)
        svg_hist1 = make_demo_svg(
            team_name, 3, 1, opp_bayern["team"], 1,
            [
                "⚽ 14' Винисиус Жуниор (пас: Родриго)",
                "⚽ 38' Килиан Мбаппе (пас: Лука Модрич)",
                "⚽ 52' Гарри Кейн",
                "⚽ 67' Винисиус Жуниор"
            ]
        )
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                player1_score, player2_score, status, photo_id, played_at
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 3, 1, 'confirmed', ?, datetime('now', '+3 hours', '-2 days'))
        """, (season_id, division_id, user_id, opp_bayern["user_id"], team_name, opp_bayern["team"], svg_hist1))
        m_hist1 = cursor.lastrowid

        svg_hist2 = make_demo_svg(
            opp_liv["team"], 2, 2, team_name, 1,
            [
                "⚽ 19' Мохамед Салах",
                "⚽ 34' Винисиус Жуниор (пас: Родриго)",
                "⚽ 61' Мохамед Салах",
                "⚽ 78' Джуд Беллингем (пас: Федерико Вальверде)"
            ]
        )
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                player1_score, player2_score, status, photo_id, played_at
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 2, 2, 'confirmed', ?, datetime('now', '+3 hours', '-1 day'))
        """, (season_id, division_id, opp_liv["user_id"], user_id, opp_liv["team"], team_name, svg_hist2))
        m_hist2 = cursor.lastrowid

        svg_hist3 = make_demo_svg(
            team_name, 2, 0, opp_barca["team"], 1,
            [
                "⚽ 41' Килиан Мбаппе (пас: Дани Карвахаль)",
                "⚽ 84' Винисиус Жуниор (пас: Родриго)"
            ]
        )
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                player1_score, player2_score, status, photo_id, played_at
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 2, 0, 'confirmed', ?, datetime('now', '+3 hours', '-3 hours'))
        """, (season_id, division_id, user_id, opp_barca["user_id"], team_name, opp_barca["team"], svg_hist3))
        m_hist3 = cursor.lastrowid

        # 11. Сыгранный матч между другими командами в Тур 1 (Барселона 2:1 Бавария)
        svg_other = make_demo_svg(
            opp_barca["team"], 2, 1, opp_bayern["team"], 1,
            [
                "⚽ 22' Роберт Левандовски (пас: Ламин Ямаль)",
                "⚽ 58' Гарри Кейн",
                "⚽ 76' Роберт Левандовски (пас: Педри)"
            ]
        )
        cursor.execute("""
            INSERT INTO matches (
                season_id, division_id, round_number, tournament_type,
                player1_id, player2_id, player1_team, player2_team,
                player1_score, player2_score, status, photo_id, played_at
            ) VALUES (?, ?, 1, 'league', ?, ?, ?, ?, 2, 1, 'confirmed', ?, datetime('now', '+3 hours', '-4 hours'))
        """, (season_id, division_id, opp_barca["user_id"], opp_bayern["user_id"], opp_barca["team"], opp_bayern["team"], svg_other))
        m_other = cursor.lastrowid

        # 12. Авторы голов и ассистов (match_events)
        events = [
            # Реал 3 : 1 Бавария
            (m_hist1, team_name, "Винисиус Жуниор", "goal", 2),
            (m_hist1, team_name, "Килиан Мбаппе", "goal", 1),
            (m_hist1, team_name, "Родриго", "assist", 1),
            (m_hist1, team_name, "Лука Модрич", "assist", 1),
            (m_hist1, opp_bayern["team"], "Гарри Кейн", "goal", 1),

            # Ливерпуль 2 : 2 Реал
            (m_hist2, team_name, "Винисиус Жуниор", "goal", 1),
            (m_hist2, team_name, "Джуд Беллингем", "goal", 1),
            (m_hist2, team_name, "Родриго", "assist", 1),
            (m_hist2, team_name, "Федерико Вальверде", "assist", 1),
            (m_hist2, opp_liv["team"], "Мохамед Салах", "goal", 2),

            # Реал 2 : 0 Барселона
            (m_hist3, team_name, "Килиан Мбаппе", "goal", 1),
            (m_hist3, team_name, "Винисиус Жуниор", "goal", 1),
            (m_hist3, team_name, "Дани Карвахаль", "assist", 1),
            (m_hist3, team_name, "Родриго", "assist", 1),

            # Барселона 2 : 1 Бавария
            (m_other, opp_barca["team"], "Роберт Левандовски", "goal", 2),
            (m_other, opp_barca["team"], "Ламин Ямаль", "assist", 1),
            (m_other, opp_barca["team"], "Педри", "assist", 1),
            (m_other, opp_bayern["team"], "Гарри Кейн", "goal", 1),
        ]

        for m_id, t_name, p_name, ev_type, count in events:
            cursor.execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, ?, ?, ?, ?)
            """, (m_id, t_name, p_name, ev_type, count))

        # 13. Обновление рейтингов Эло на основе сыгранных матчей
        database.update_team_elo(team_name, division_id, season_id, 1545.0)
        database.update_team_elo(opp_barca["team"], division_id, season_id, 1510.0)
        database.update_team_elo(opp_liv["team"], division_id, season_id, 1505.0)
        database.update_team_elo(opp_bayern["team"], division_id, season_id, 1455.0)
        database.update_team_elo(opp_city["team"], division_id, season_id, 1500.0)
        database.update_team_elo(opp_arsenal["team"], division_id, season_id, 1500.0)
        database.update_team_elo(opp_inter["team"], division_id, season_id, 1500.0)

        # 14. Кошелек пользователя
        cursor.execute("""
            INSERT INTO user_wallets (user_id, balance, bets_count, bets_won, updated_at)
            VALUES (?, 1000, 5, 3, datetime('now', '+3 hours'))
            ON CONFLICT(user_id) DO UPDATE SET balance = MAX(balance, 1000)
        """, (user_id,))

    # 14. Генерация коэффициентов и рынков для туров 1 и 2
    try:
        generate_round_markets(1, division_id=division_id, season_id=season_id)
        generate_round_markets(2, division_id=division_id, season_id=season_id)
        print("   📊 Рынки ставок для Тура 1 и Тура 2 успешно рассчитаны!")
    except Exception as e:
        print(f"   ⚠️ Ошибка генерации рынков ставок: {e}")

    print("\n✅ Тестовые данные успешно созданы в базе данных!")
    print("📋 Что теперь доступно в приложении:")
    print("   1. «Турнир» -> «Бомбардиры»: полный список топ-бомбардиров (Винисиус 4, Левандовски 2, Кейн 2, Салах 2, Мбаппе 2) и ассистентов.")
    print("   2. «Линия»: вкладки Тур 1 и Тур 2 активны; матчи между сторонними клубами (Бавария-Интер, Арсенал-Ливерпуль) с коэффициентами.")
    print("   3. Фильтры линии: «⚡ Все», «🔥 Открытые», «⏰ Скоро» (Тур 2), «✅ Завершённые» (со счетом).")
    print("   4. Поиск: ищет по командам в линии и фильтрует блок персональных рекомендаций.")
    print("   5. «Мой Клуб» -> «История игр»: клик по матчу открывает модалку с карточкой, авторами голов и скриншотом.")
    print("   6. «Кабинет» (профиль): блок турнирной статистики удалён.")


def find_candidate_user_ids() -> list[dict]:
    """Найти реальных пользователей из БД для удобной привязки демо-клуба."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM user_wallets WHERE user_id NOT IN (990101, 990102, 990103, 990104, 990105, 990106)
                UNION
                SELECT telegram_id as user_id FROM users WHERE telegram_id NOT IN (990101, 990102, 990103, 990104, 990105, 990106)
            ) ORDER BY user_id DESC
        """)
        rows = cursor.fetchall()
        candidates = []
        for r in rows:
            uid = r["user_id"]
            cursor.execute("SELECT username, team_name FROM users WHERE telegram_id = ?", (uid,))
            u_info = cursor.fetchone()
            candidates.append({
                "user_id": uid,
                "username": u_info["username"] if u_info and u_info["username"] else None,
                "team_name": u_info["team_name"] if u_info and u_info["team_name"] else None
            })
        return candidates


def main():
    parser = argparse.ArgumentParser(description="Seed comprehensive demo data for Logovo.bet")
    parser.add_argument("--user-id", type=int, default=None, help="Telegram ID пользователя для привязки клуба")
    parser.add_argument("--team", type=str, default="Реал Мадрид", help="Название клуба (default: Реал Мадрид)")
    parser.add_argument("--division-id", type=int, default=1, help="ID дивизиона (default: 1)")
    parser.add_argument("--clean", action="store_true", help="Очистить предыдущие демо-данные перед генерацией")
    parser.add_argument("--clean-only", "--purge", action="store_true", help="Только очистить базу от тестовых данных (без генерации)")
    parser.add_argument("--list-users", action="store_true", help="Показать список пользователей в БД")

    args = parser.parse_args()

    candidates = find_candidate_user_ids()

    if args.list_users:
        print("\n🔍 Найденные пользователи в базе данных:")
        if not candidates:
            print("   (реальные пользователи пока не найдены)")
        for idx, c in enumerate(candidates, start=1):
            un = f"@{c['username']}" if c['username'] else "(без username)"
            tm = f"| Клуб: {c['team_name']}" if c['team_name'] else "| [Без клуба]"
            print(f"   {idx}. Telegram ID: {c['user_id']} | {un} {tm}")
        print("\nДля привязки клуба к конкретному пользователю запустите:")
        print("   python scripts/seed_my_club_demo.py --user-id ВАШ_ID\n")
        return

    target_user_id = args.user_id
    if not target_user_id:
        if candidates:
            # Ищем кандидата с уже привязанным демо-клубом (например, Реал Мадрид)
            match_team = [c for c in candidates if c.get("team_name") and c["team_name"].lower() == args.team.lower()]
            if match_team:
                target_user_id = match_team[0]["user_id"]
            else:
                target_user_id = candidates[0]["user_id"]
            c_info = next((c for c in candidates if c["user_id"] == target_user_id), candidates[0])
            un = f" (@{c_info['username']})" if c_info['username'] else ""
            print(f"🎯 Выбран пользователь: Telegram ID {target_user_id}{un}")
        elif config.ADMIN_IDS:
            target_user_id = config.ADMIN_IDS[0]
            print(f"🎯 Выбран ID администратора из config.ADMIN_IDS: {target_user_id}")
        else:
            target_user_id = 1642770076
            print(f"🎯 Выбран ID по умолчанию: {target_user_id}")

    if args.clean_only:
        purge_demo_data(
            user_id=target_user_id,
            team_name=args.team,
            division_id=args.division_id
        )
        return

    seed_cabinet_demo(
        user_id=target_user_id,
        team_name=args.team,
        division_id=args.division_id,
        clean=args.clean
    )


if __name__ == "__main__":
    main()
