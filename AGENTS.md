# AGENTS.md — logovobot

Rules for agents working in this repository. Read `CLAUDE.md` first: it owns the
architecture, domain model, storage rules, conventions and commands. This file does not
restate it — it adds **scope**, **working rules** and a set of **traps** that are not
obvious from the code. Where the two disagree on architecture, `CLAUDE.md` wins; where they
disagree on scope, this file wins.

---

## Working mode

- **Study before editing.** On a mapping/reconnaissance pass: read only, change nothing, and
  report structure, components, dependencies, file→function ownership and risks — not a diff.
- **Never build a second system for a rule that already has one.** Business invariants in this
  repo are deliberately implemented once and shared (see the single-source table below). A
  parallel implementation forks the invariant silently — `reports/FIX_01_*`, `FIX_03_*` and
  `FIX_04_*` exist precisely because duplicated checks had drifted apart. If the hook you need
  does not exist, extend the existing path rather than adding a second entry point.
- **Do not delete existing functionality.** No drive-by cleanups, no refactor-for-refactor, no
  renaming unused variables to silence linters, no compatibility shims for code you are
  removing. Dead-looking code that is still present is history — explain it, do not delete it.
- **Do not widen scope.** One reported problem, one fix.
- Commits follow Conventional Commits. Work lands directly on `main` (see `CLAUDE.md`).

---

## Scope

### Out of perimeter — Logovo Tracker

A separate subsystem (Telegram logistics for the match-operation team). Do not analyse,
audit, report vulnerabilities in, or modify:

- `api/routes_tracker.py`, `handlers/tracker.py`
- the `/tracker` and `/app` commands
- PIN → session logic, Tracker OCR, `tests/test_tracker_api.py`

Tracker-only findings are **not** reported as project risks.

### In perimeter — LIVE

`api/routes_live.py`, `api/routes_admin_live.py`, the readers in
`services/live_ingestion.py`, the tables `live_match_states` / `live_events` /
`live_statistics`, `services/feature_engine.py` (xG) → `ensemble_engine`, `odds_movers`,
`recommendation_engine`, and the season scripts that truncate those tables — all count as
working product code.

Known and accepted: the only production writer of the live tables is the manual correction
path in `routes_admin_live`. `live_state_machine.transition_live_match` and
`sports/odds_sync.sync_provider_odds` are implemented and tested but not called from the
product. That is a design assumption, not a defect to fix on someone else's ticket.

### Untouchable Tracker seams

Tracker code is welded into shared modules. Leave every one of these exactly as it is — do
not refactor, do not log as tech debt, simply do not consider it:

- `api/server.py:65-71`, `:254-256`, `:439-444`
- `api/rate_limiter.py:164-177`, `:224-239`, `:253-255`
- `config.py:222-234`
- `handlers/__init__.py:403-405`

---

## Traps

1. **`tracker` names two unrelated things.** `job_debt_lifecycle_tracker` (`handlers/admin.py`,
   registered in `main.py`) is the discipline **debt** tracker — reminders, auto-warns,
   auto-kick. It has nothing to do with Logovo Tracker. A case-insensitive grep for "tracker"
   hits both. Never let a "clean up tracker" change touch it.
2. **`.claude/worktrees/` holds ~13 stale copies of the whole repository**, including copies of
   `database.py` and every handler. Every `grep`/`find` must exclude `.claude/`, `venv/`,
   `.agents/`. Editing a worktree copy is the easiest way to lose work here.
3. **Callback-data near-twins:** `admin_confirm_delete_player_{id}` and
   `admin_delete_player_confirm_{id}` are different parsers on different actions. The comment at
   `handlers/__init__.py:748-749` warns they must not share a pattern.
4. **Handler registration order is load-bearing.** Anything registered after the AI-chat
   catch-all `MessageHandler` / catch-all `CallbackQueryHandler` in group 0 can never fire.
   New handlers go *before* them.
5. **`is_admin` passes any division admin.** Per-object scoping is a *separate*, manual check
   inside each handler (`_ensure_division_access` / `_ensure_match_access` in `handlers/admin.py`,
   or `database.is_division_admin`). Handlers taking a raw `player_id`/`user_id` from
   callback_data without one grant cross-division reach.
6. **Documentation drifts; code does not.** Line numbers in `reports/FIX_0*.md` are stale (the
   betting gate is cited at `:4452`, actually ~`5937`), and the SQL quoted in `FIX_01` §3 is an
   older, *more permissive* version than what ships. `CLAUDE.md` mentions a `rounds_v` view that
   does not exist (it was a temporary rebuild table). `.agents/AGENTS.md` and `.claude/prds/` are
   self-declared stale. Trust the file, verify the line.
7. **In-chat betting is deprecated but still present.** `register_betting_handlers` wires only
   the redirect to the Mini App; the whole slip-building engine in `handlers/betting.py`
   (`cmd_bet_hub` → `cb_bet_add_outcome` → `cb_bet_place_amount`) is unregistered. Do not build
   new features on it.

---

## Single sources of truth — route through these

| Need | The one place |
|---|---|
| "Can a bet still be placed?" | `database.evaluate_round_betting_gate` |
| Placing a bet (Telegram + Mini App + REST) | `database.place_user_bet` |
| Bet limits (user → division → global) | `services/betting_limits.BettingLimitsService` + `risk_limits_config` |
| Risk gate | `services/risk_engine.RiskEngine.evaluate_bet` (fail-closed at the call site) |
| Market settlement rules | `services/market_settler.evaluate_market_selection` (pure function) |
| Payouts / wallet ledger | `services/settlement_engine` + typed `coin_transactions` |
| Division scoping | `resolve_division_target`, `_ensure_division_access`, `_ensure_match_access` |
| Feature access / lockdown | `handlers.base.is_logovo_access_allowed` |
| Time | `time_utils.now_msk()` / `now_msk_str()` / `today_msk()` / `SQL_NOW` |
| Club identity | `club_registry.resolve_team_name[_ex]` |
| Clubs of a division | `database.get_division_teams` |
| Division topic routing | `services/topic_cache` (+ `reload_cache()` after mutations) |
| All SQL | `database.py` only |

---

## Constraints that will fail the build if ignored

- **One clock (MSK).** `tests/test_msk_time.py` statically scans `api/ handlers/ services/
  scripts/ utils/ database.py main.py` and forbids `CURRENT_TIMESTAMP`/`CURRENT_DATE`/
  `CURRENT_TIME`, a bare `'now'` in `datetime|date|julianday|strftime` without `'+3 hours'`, and
  `datetime.now()`/`utcnow()`/`date.today()` outside `time_utils`. It also fails **any `INSERT`
  into a table with a DEFAULT timestamp that does not name that column** — this is the rule new
  betting SQL breaks most often.
- **Migrations are additive.** `CREATE TABLE IF NOT EXISTS` + a `schema_migrations` guard row.
  Never rewrite or drop a deployed table; SQLite cannot change a column DEFAULT without a
  rebuild.
- **`transaction()` is re-entrant and the connection is thread-local.** Do not start a
  `asyncio.to_thread(...)` inside an open `transaction()` — see the deliberate workaround at
  `api/routes_admin_live.py:357-358`.
- **Tests are per-module isolated.** `conftest.py` gives every test file its own temp
  `league.db`; `pytest.ini` runs `-n auto --dist loadfile`. Never depend on cross-file state,
  never reorder numbered tests inside a file, and restore `database.DB_PATH` if a test repoints
  it. Real `database.py` against the temp DB is the house style — do not mock the DB.
- **New API routes belong in `create_app()`** — `tests/test_api_route_matrix.py` harvests the
  inventory from it, so registration gets 401/403/IDOR coverage for free.
- **Single-process assumptions are real:** `_bet_placement_lock` is a `threading.RLock`, the API
  rate limiter is in-memory, and `draft_tasks` / `draft_media_groups` / `_bet_in_flight` are
  process-local. Nothing here survives a second worker.

---

## Known unprotected invariants

Flag these if a change touches them; they are the ones with no test behind them.

1. The `matches.division_id IS NULL ⇒ division 1` convention (`COALESCE(division_id, 1)` in 11
   places, `else 1` at `database.py:9063`) — **no test asserts it**; changing the literal passes
   the entire suite today.
2. `close_round_betting_line`, `reopen_round_betting_line`, `advance_betting_line_pair`,
   `match_line_is_open`, `log_betting_audit`, `_round_scope_divisions` — no direct tests, and
   their explicit-scope guard errors are unverified.
3. Cutoff and settlement use **different boundaries**: the gate compares `rounds.deadline`,
   settlement keys on `matches.status IN ('confirmed','completed')`. `matches.match_time` is not
   part of the cutoff.
4. Money is written in two places: `settlement_engine`, and a refund loop that bypasses it at
   `api/routes_admin_live.py:344-383`.
5. Constants are duplicated: `_MAX_BET`/`_MAX_PAYOUT` hardcoded in `database.py` vs
   `risk_limits_config`; default odds and bet limits re-implemented in `web/js/ui.js` and
   `web/js/store.js`; `TEAM_LOGO_MAP` duplicated Python/JS with a roster test on the Python side
   only.
6. `check_user_access` is never called in `api/routes_tournaments.py` or
   `api/routes_user_extras.py`, and `user_extras` mutates state (saved coupons, favorites,
   notifications).
7. `api/routes_matches.py:224` (`/api/matches/{id}/photo`) is unauthenticated and takes a
   client-supplied `photo_id`.
8. Latent bug, currently unreachable: `services/sports/odds_sync.py:189` calls
   `detect_odds_anomalies(match_id)` while the signature is `division_id`
   (`services/odds_movers.py:210`). It will fire the moment a provider feed is wired up.
9. Unregistered import: `handle_leaderboard` is imported at `api/server.py:16` but never added
   to the router — `/api/leaderboard` is served by `routes_gamification.py` instead.
