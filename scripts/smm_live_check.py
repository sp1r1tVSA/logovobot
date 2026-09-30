"""Live check of the club SMM post generation against real models.

Run it where the real `.env` lives (the server):

    python scripts/smm_live_check.py                       # 3 clubs (win/loss/draw) x 5 post types
    python scripts/smm_live_check.py --clubs Бавария ПСЖ --types recap,matchday
    python scripts/smm_live_check.py --stages              # also one cup stage and one league round
    python scripts/smm_live_check.py --dry                 # no network: stub models, checks the script itself

For every request it prints each raw model answer (provider, model, length), what
`validate_post` said about it, and the text that would be shown to the coach — so both
prompt quality and validator false positives can be judged by eye. Read-only: it only
calls the same SELECT-based payload builders the bot uses, and never publishes anything.
Free models are rate-limited, hence the pause between requests (--pause).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (loads .env)
from services import club_smm_service as S  # noqa: E402

POST_TYPES = ("matchday", "recap", "standings", "spotlight", "custom")
DEFAULT_BRIEF = "Начало нового сезона: настрой, амбиции, ждём болельщиков"


def _pick_clubs(limit_scan: int = 60) -> list[str]:
    """One club per last-match outcome (win / loss / draw), so every tone gets exercised."""
    wanted = {"win": None, "loss": None, "draw": None}
    for club in list(config.CLUB_REGISTRY)[:limit_scan]:
        try:
            res = (S.get_club_smm_payload(club).get("last_match") or {}).get("result")
        except Exception:
            continue
        if res in wanted and wanted[res] is None:
            wanted[res] = club
        if all(wanted.values()):
            break
    return [c for c in wanted.values() if c]


class Recorder:
    """Wraps the two provider calls and records every raw answer with its validator verdict."""

    def __init__(self, dry: bool):
        self.dry = dry
        self.attempts: list[dict] = []
        self._orig_or = S._call_openrouter_text
        self._orig_gem = S._call_gemini_text
        S._call_openrouter_text = lambda *a, **k: self._call("openrouter", self._orig_or, a, k)
        S._call_gemini_text = lambda *a, **k: self._call("gemini", self._orig_gem, a, k)

    def _call(self, provider, orig, args, kwargs):
        if self.dry:
            # Deliberately invalid on the first provider (invented score), fine on the second.
            text = ("<b>⚽🔥 Тест</b>\nСчёт 9:9 — выдумка. #Тест" if provider == "openrouter"
                    else "<b>⚽🔥 Тест</b>\nКороткий чистый пост без счёта. #Тест")
            model = "dry-run"
        else:
            text, model = orig(*args, **kwargs)
        self.attempts.append({"provider": provider, "model": model, "text": text or ""})
        return text, model

    def reset(self):
        self.attempts = []


def _report(rec: Recorder, payload: dict, kind: str, final: str, extra_text: str = "") -> dict:
    print(f"  попыток провайдеров: {len(rec.attempts)}")
    stats = {"attempts": len(rec.attempts), "rejected": 0, "empty": 0}
    for i, a in enumerate(rec.attempts, 1):
        if not a["text"]:
            stats["empty"] += 1
            print(f"  [{i}] {a['provider']}: пусто (ключ/квота/таймаут)")
            continue
        fitted = S._fit_html(a["text"], S.POST_MAX_CHARS)
        problems = S.validate_post(fitted, payload, kind, extra_text)
        if problems:
            stats["rejected"] += 1
        verdict = "OK" if not problems else "ОТКЛОНЁН: " + "; ".join(problems)
        print(f"  [{i}] {a['provider']} ({a['model']}), {len(a['text'])} симв. → {verdict}")
        print("      " + a["text"].replace("\n", "\n      "))
    used_template = not any(
        a["text"] and not S.validate_post(S._fit_html(a["text"], S.POST_MAX_CHARS), payload, kind, extra_text)
        for a in rec.attempts
    )
    stats["template"] = used_template
    print(f"  ИТОГ ({'шаблон-фолбэк' if used_template else 'ответ модели'}), {len(final)} симв.:")
    print("      " + final.replace("\n", "\n      "))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clubs", nargs="*", help="club names; default: one club per win/loss/draw")
    ap.add_argument("--types", default=",".join(POST_TYPES), help="comma-separated post types")
    ap.add_argument("--brief", default=DEFAULT_BRIEF, help="brief for the 'custom' post type")
    ap.add_argument("--stages", action="store_true", help="also generate one cup-stage and one league-round post")
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between requests (free-tier limits)")
    ap.add_argument("--dry", action="store_true", help="stub the models, do not touch the network")
    args = ap.parse_args()

    if not args.dry and not (
        getattr(config, "OPENROUTER_API_KEY", "") or getattr(config, "GEMINI_API_KEY", "")
        or getattr(config, "GEMINI_CHAT_API_KEY", "")
    ):
        print("Нет ни OPENROUTER_API_KEY, ни GEMINI_*_API_KEY в config — запусти на сервере с .env.")
        return 2

    clubs = args.clubs or _pick_clubs()
    if not clubs:
        print("Не нашёл клубов с сыгранными матчами.")
        return 1
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    rec = Recorder(args.dry)
    totals = {"requests": 0, "attempts": 0, "rejected": 0, "empty": 0, "template": 0}

    def bump(st):
        totals["requests"] += 1
        totals["attempts"] += st["attempts"]
        totals["rejected"] += st["rejected"]
        totals["empty"] += st["empty"]
        totals["template"] += int(st["template"])

    for club in clubs:
        base_payload = S.get_club_smm_payload(club)
        res = (base_payload.get("last_match") or {}).get("result")
        for pt in types:
            print(f"\n=== {club} | {pt} | последний матч: {res} ===")
            rec.reset()
            brief = args.brief if pt == "custom" else ""
            final = S.generate_club_post(club, pt, custom_brief=brief)
            payload = S.get_club_smm_payload(club)
            bump(_report(rec, payload, S._tone_kind(pt, payload), final, brief))
            time.sleep(0 if args.dry else args.pause)

        if args.stages:
            st = S.get_club_stages_and_rounds(club)
            targets = []
            if st.get("cup_stages"):
                targets.append(("cup", st["cup_stages"][0]))
            if st.get("league_rounds"):
                targets.append(("round", st["league_rounds"][0]))
            for kind_name, item in targets:
                print(f"\n=== {club} | stage-{kind_name} | {item} ===")
                rec.reset()
                try:
                    if kind_name == "cup":
                        final = S.generate_stage_post(
                            club, cup_stage=item.get("stage"), cup_division_id=item.get("cup_scope"))
                    else:
                        final = S.generate_stage_post(club, round_number=item.get("round"))
                except Exception as exc:  # report and go on, this is a diagnostic tool
                    print(f"  ОШИБКА: {exc!r} (структура элемента: {sorted(item)})")
                    continue
                print(f"  ИТОГ: {final}")
                totals["requests"] += 1
                totals["attempts"] += len(rec.attempts)
                time.sleep(0 if args.dry else args.pause)

    print("\n=== СВОДКА ===")
    print(f"запросов: {totals['requests']}, вызовов провайдеров: {totals['attempts']}, "
          f"отклонено валидатором: {totals['rejected']}, пустых ответов: {totals['empty']}, "
          f"ушло в шаблон: {totals['template']}")
    print("Смотри: (1) отклонённые ответы — это настоящая ошибка модели или ложное срабатывание валидатора?; "
          "(2) читаются ли итоговые тексты в нужном тоне; (3) сколько запросов упало в шаблон.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
