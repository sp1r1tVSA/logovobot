"""
services/market_settler.py

Logovo.bet — Market Settlement Engine & Rule Evaluator.
Pure, deterministic evaluation of match outcomes against market selections.
"""

from typing import Literal

OutcomeResult = Literal["won", "lost", "voided", "refunded"]


def evaluate_market_selection(
    market_key: str,
    selection_key: str,
    score1: int,
    score2: int,
    match_status: str = "finished",
    ht_score1: int | None = None,
    ht_score2: int | None = None,
    winner_side: str | None = None
) -> OutcomeResult:
    """
    Evaluate whether a selection outcome is won, lost, voided, or refunded.

    `winner_side` ('p1' | 'p2' | None) — поле победителя ИГРЫ для матчей, где
    ничьей не бывает. В общем кубке основное время может кончиться 2:2 и
    разрешиться послематчевыми, поэтому «П1» там означает «клуб 1 взял игру», а
    не «клуб 1 забил больше». Для всех прочих рынков (тоталы, ОЗ, инд. тоталы,
    фора, точный счёт) основанием остаётся счёт основного времени: «ТМ2.5»
    проигрывает при 2:2 независимо от того, кто выиграл по послематчевым.
    """
    if match_status in ("cancelled", "voided"):
        return "voided"

    if match_status not in ("finished", "completed"):
        raise ValueError(f"Cannot settle match in status '{match_status}'")

    s1 = int(score1)
    s2 = int(score2)
    tot = s1 + s2

    # 1. 1X2 (Match Winner)
    if market_key in ("1x2", "match_winner", "outcome", "match_result"):
        if selection_key in ("p1", "1", "home"):
            if winner_side is not None:
                return "won" if winner_side == "p1" else "lost"
            return "won" if s1 > s2 else "lost"
        if selection_key in ("x", "draw"):
            return "won" if s1 == s2 else "lost"
        if selection_key in ("p2", "2", "away"):
            if winner_side is not None:
                return "won" if winner_side == "p2" else "lost"
            return "won" if s2 > s1 else "lost"

    # 2. Double Chance
    elif market_key == "double_chance":
        if selection_key in ("1x", "dc_1x"):
            return "won" if s1 >= s2 else "lost"
        if selection_key in ("12", "dc_12"):
            return "won" if s1 != s2 else "lost"
        if selection_key in ("x2", "dc_x2"):
            return "won" if s2 >= s1 else "lost"

    # 3. Total Goals (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)
    elif market_key in ("total_goals", "totals", "over_under"):
        # Legacy support
        if selection_key in ("tb25", "over_2.5"):
            return "won" if tot > 2.5 else "lost"
        if selection_key in ("tm25", "under_2.5"):
            return "won" if tot < 2.5 else "lost"

        if selection_key.startswith("over_"):
            threshold = float(selection_key.replace("over_", ""))
            return "won" if tot > threshold else "lost"
        if selection_key.startswith("under_"):
            threshold = float(selection_key.replace("under_", ""))
            return "won" if tot < threshold else "lost"

    # 4. Both Teams to Score (BTTS)
    elif market_key in ("btts", "both_teams_to_score"):
        if selection_key in ("btts_yes", "yes"):
            return "won" if (s1 > 0 and s2 > 0) else "lost"
        if selection_key in ("btts_no", "no"):
            return "won" if (s1 == 0 or s2 == 0) else "lost"

    # 5. Individual Total 1 (Home)
    elif market_key == "individual_total_1":
        if selection_key.startswith("it1_over_"):
            threshold = float(selection_key.replace("it1_over_", ""))
            return "won" if s1 > threshold else "lost"
        if selection_key.startswith("it1_under_"):
            threshold = float(selection_key.replace("it1_under_", ""))
            return "won" if s1 < threshold else "lost"

    # 6. Individual Total 2 (Away)
    elif market_key == "individual_total_2":
        if selection_key.startswith("it2_over_"):
            threshold = float(selection_key.replace("it2_over_", ""))
            return "won" if s2 > threshold else "lost"
        if selection_key.startswith("it2_under_"):
            threshold = float(selection_key.replace("it2_under_", ""))
            return "won" if s2 < threshold else "lost"

    # 7. Handicap
    elif market_key == "handicap":
        if selection_key == "h1_minus_1.5":
            return "won" if (s1 - 1.5) > s2 else "lost"
        if selection_key == "h2_plus_1.5":
            return "won" if (s2 + 1.5) > s1 else "lost"
        if selection_key == "h2_minus_1.5":
            return "won" if (s2 - 1.5) > s1 else "lost"
        if selection_key == "h1_plus_1.5":
            return "won" if (s1 + 1.5) > s2 else "lost"

    # 8. Draw No Bet (DNB)
    elif market_key == "draw_no_bet":
        if s1 == s2:
            return "voided"
        if selection_key in ("dnb_1", "1"):
            return "won" if s1 > s2 else "lost"
        if selection_key in ("dnb_2", "2"):
            return "won" if s2 > s1 else "lost"

    # 9. Correct Score
    elif market_key == "correct_score":
        expected_score = selection_key.replace("cs_", "").replace("_", ":")
        actual_score = f"{s1}:{s2}"
        return "won" if actual_score == expected_score else "lost"

    # 10. Half-Time Result
    elif market_key in ("ht_result", "first_half", "1st_half"):
        if ht_score1 is None or ht_score2 is None:
            return "voided"
        if selection_key in ("ht_p1", "p1", "1"):
            return "won" if ht_score1 > ht_score2 else "lost"
        if selection_key in ("ht_x", "x", "draw"):
            return "won" if ht_score1 == ht_score2 else "lost"
        if selection_key in ("ht_p2", "p2", "2"):
            return "won" if ht_score2 > ht_score1 else "lost"

    # 11. Second-Half Result
    elif market_key in ("second_half", "2nd_half_result"):
        if ht_score1 is None or ht_score2 is None:
            return "voided"
        sh1 = s1 - ht_score1
        sh2 = s2 - ht_score2
        if selection_key in ("sh_p1", "p1", "1"):
            return "won" if sh1 > sh2 else "lost"
        if selection_key in ("sh_x", "x", "draw"):
            return "won" if sh1 == sh2 else "lost"
        if selection_key in ("sh_p2", "p2", "2"):
            return "won" if sh2 > sh1 else "lost"

    # 12. Next Goal
    elif market_key == "next_goal":
        if selection_key in ("no_goal", "none"):
            return "won" if (s1 + s2) == 0 else "lost"
        if selection_key in ("next_team1", "team1", "p1"):
            return "won" if s1 > 0 else "lost"
        if selection_key in ("next_team2", "team2", "p2"):
            return "won" if s2 > 0 else "lost"

    # 13. Corners Total (over / under)
    elif market_key == "corners":
        if selection_key.startswith("over_"):
            try:
                thresh = float(selection_key.replace("over_", ""))
                # If match statistics corners unavailable, fail-safe to voided
                return "voided"
            except ValueError:
                return "voided"
        if selection_key.startswith("under_"):
            try:
                thresh = float(selection_key.replace("under_", ""))
                return "voided"
            except ValueError:
                return "voided"

    # 14. Cards Total (over / under)
    elif market_key == "cards":
        if selection_key.startswith("over_") or selection_key.startswith("under_"):
            return "voided"

    # Unknown market/outcome type -> fail safe to void
    return "voided"
