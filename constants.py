"""Constants for logovobot callback_data strings and configuration defaults."""

# ─── Общий кубок ──────────────────────────────────────────────────────────────
# Стадии плей-офф в порядке игры. `CUP_STAGE_ORDER` — позиция в сетке (1 = 1/64),
# по ней стадия сортируется и идёт в порядок линии этапа.
CUP_STAGES: tuple[str, ...] = ("1/64", "1/32", "1/16", "1/8", "1/4", "1/2", "final")
CUP_STAGE_ORDER: dict[str, int] = {stage: i + 1 for i, stage in enumerate(CUP_STAGES)}

# Серия играется до двух побед. Три игры заводятся сразу, потому что линия
# открывается на весь этап и закрывается его стартом: игру 2, появившуюся после
# счёта 1:0, открыть для ставок было бы уже нечем. Несыгранные игры серии,
# закончившейся 2:0, аннулируются при закрытии серии.
CUP_SERIES_GAMES = 3

# Роли тем в кубковой группе. Отдельно от `division_topics`, потому что кубок —
# не дивизион: 'line' — опубликованная линия этапа, 'reports' — результаты и
# отчёты по сыгранным сериям.
CUP_TOPIC_TYPES: tuple[str, ...] = ("line", "reports")

# Кубковый матч не принадлежит ни одному дивизиону, а `matches.division_id IS NULL`
# в проекте означает «дивизион 1» (COALESCE(division_id, 1) в запросах линии, долгов
# и отчётов). Sentinel 0 не совпадает ни с одной строкой `divisions`, поэтому лиговые
# джойны отсекают кубковые матчи сами, без правки в каждом из них.
CUP_DIVISION_SENTINEL = 0

# Main Menu & Base Navigation
CB_MAIN_MENU = "main_menu"
CB_MENU_CABINET = "menu_cabinet"
CB_MENU_DIVISIONS = "menu_divisions"
CB_MENU_SUPPORT = "menu_support"

# User Cabinet Section
CB_CABINET_MY_MATCHES = "cabinet_my_matches"
CB_CABINET_MY_SQUAD = "cabinet_my_squad"
CB_CABINET_CLUB_STATS = "cabinet_club_stats"
CB_CABINET_GAME_HISTORY = "cabinet_game_history"
CB_CABINET_UPLOAD_SQUAD = "cabinet_upload_squad"

# Player Card
CB_PLAYER_CARD = "player_card_"

# Admin Navigation & Actions
CB_ADMIN_MAIN_MENU = "admin_main_menu"
CB_ADMIN_MANAGE_PLAYERS = "admin_manage_players"
CB_ADMIN_CANCEL_MATCH_ACTION = "admin_cancel_match_action"
CB_ADMIN_CANCEL_PLAYER_ACTION = "admin_cancel_player_action"
CB_ADMIN_BROADCAST_STUB = "admin_broadcast_stub"
