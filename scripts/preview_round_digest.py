"""Превью round_digest_generator — итоги тура с результатами матчей."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.graphics.round_digest_generator import generate_round_digest_image

payload = {
    "round_number": 7,
    "division_name": "Логово Фифарей — Лига",
    "division_id": 1,
    "matches_played": 4,
    "goals_total": 18,
    "results": [
        {"team1": "Бешикташ",   "score1": 4, "score2": 1, "team2": "Реал Мадрид",   "match_id": 1, "mvp_player": "Иванов А."},
        {"team1": "Барселона",  "score1": 2, "score2": 2, "team2": "Манчестер Сити","match_id": 2, "mvp_player": ""},
        {"team1": "ПСЖ",        "score1": 1, "score2": 3, "team2": "Ливерпуль",      "match_id": 3, "mvp_player": "Смирнов К."},
        {"team1": "Атлетико",   "score1": 5, "score2": 0, "team2": "Боруссия Д",    "match_id": 4, "mvp_player": "Петров Д."},
    ],
    "player_of_the_round": {
        "player_name": "Петров Д.",
        "team_name": "Атлетико",
        "goals": 3,
        "assists": 2,
    },
    "mvp_of_the_round": {
        "player_name": "Петров Д.",
        "mvp_count": 2,
    },
    "rout": {
        "team1": "Атлетико",
        "score1": 5,
        "score2": 0,
        "team2": "Боруссия Д",
        "match_id": 4,
        "margin": 5,
    },
    "movers": [
        {"team": "Бешикташ",  "position": 1, "previous_position": 3, "movement": 2,  "points": 19},
        {"team": "Ливерпуль", "position": 2, "previous_position": 1, "movement": -1, "points": 18},
        {"team": "Атлетико",  "position": 3, "previous_position": 4, "movement": 1,  "points": 16},
    ],
}

buf = generate_round_digest_image(payload)
fname = "preview_round_digest.png"
with open(fname, "wb") as f:
    f.write(buf.read())
print(f"✅ Сохранено: {os.path.abspath(fname)} ({os.path.getsize(fname)//1024} KB)")
