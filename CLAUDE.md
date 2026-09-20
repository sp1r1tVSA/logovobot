# CLAUDE.md — logovobot

Guidance for Claude Code when working in this repository.

**Logovobot** (Логово Фифарей / ИИ «Темшик») is an async Telegram bot that runs FIFA/FC
e-sports championships: divisions and rounds, match result intake via AI screenshot OCR,
standings and Pillow-rendered infographics, a debt/warn discipline system, and a virtual
prediction market ("Logovo.bet") exposed through a Telegram Mini App.

The project is well past MVP — 249 Python files (118 application modules + 131 pytest
files), 59 SQLite tables, and ten completed development phases documented in the
`PHASE_*.md` reports under `reports/`. Post-phase work is logged in the numbered
`FIX_*.md` notes and the `*_AUDIT.md` reports beside them.

---

## Stack

- Python 3.11+, `python-telegram-bot[job-queue]` v21 (fully async)
- SQLite in WAL mode — single file, no ORM, hand-written SQL
- Google Gemini via **raw REST over `aiohttp`** — there is no Google SDK dependency
- Pillow + `pillow-heif` + `opencv-python-headless` + `numpy` for graphics and image prep
- `aiohttp` also serves the Mini App API; deployed as a single `worker: python main.py`

`requirements.txt` is runtime-only — the worker installs just that file, so no test tooling
may be added to it. `requirements-dev.txt` pulls it in via `-r` and adds `pytest` and
`pytest-xdist`. Deployment artefacts at the root: `Procfile` (`worker: python main.py`),
`Dockerfile`, `.dockerignore`.

---

## Commands

Run tests:

```bash
python -m pytest tests/ -q
```

Run a single test file:

```bash
python -m pytest tests/test_divisions_schema.py -v
```

Run the bot (needs a populated `.env`):

```bash
python main.py
```

Run serially when debugging shared state:

```bash
python -m pytest tests/ -n0
```

Install the dev dependencies first:

```bash
pip install -r requirements-dev.txt
```

`pytest.ini` is the only config (there is no `pyproject.toml` / `setup.cfg`). It sets
`testpaths = tests` and `addopts = -q -n auto --dist loadfile`, so runs are parallel across
cores by default, with each file pinned to one worker. Tests that share module or class
state within a file therefore keep their order. `markers` declares `slow`, excluded via
`-m "not slow"`.

The repo-root `conftest.py` is the test bootstrap and does the DB isolation, because
`config.py` resolves `DB_PATH` at import time:

- it sets `LEAGUE_SQLITE_PATH` to a per-process temp file before anything imports `config`,
  and provides placeholder `TELEGRAM_BOT_TOKEN` / `ADMIN_IDS` via `setdefault` for the
  module-level `from config import …` in many test files;
- a module-scoped autouse fixture then gives **every test module its own** `league.db` and
  calls `init_db()` on it, so files are not order-dependent and the repo-root `league.db`
  is never touched;
- further autouse fixtures disable API rate limiting, keep `config.ADMIN_IDS` the same list
  object across the process, and drop the cached per-thread SQLite connection around each
  test (needed on Windows, where an open handle silently blocks file deletion).

CI is `.github/workflows/tests.yml` — Python 3.11, `pip install -r requirements-dev.txt`,
`python -m pytest tests/` on pushes to `main` and on every PR. It passes deliberately fake
secrets as env vars; real values live only in the server `.env` and must never become
repository secrets.

---

## Architecture

`main.py` is thin: configure logging → `init_db()` → build `ApplicationBuilder` →
`register_all_handlers()` → `register_jobs()` → `run_polling()`. The Mini App API server
is started from `post_init` as a background task, wrapped in try/except so an API failure
never prevents the bot itself from starting. Preserve that isolation.

| Path | Responsibility |
|---|---|
| `main.py` | Entrypoint, `post_init`, background job registration |
| `config.py` | All env parsing. Every setting must be read here, never via `os.getenv` at a call site |
| `database.py` | ~10.5k lines: schema, migrations, and every repository function |
| `constants.py` | Shared enums and literals |
| `club_registry.py` | Canonical club names, aliases, and the tiered name resolver. Imports `config` only |
| `conftest.py` | Test bootstrap: per-module temp SQLite DB and the autouse fixtures (see Commands) |
| `handlers/` (11 modules) | Telegram entrypoints — `admin`, `cabinet`, `drafts`, `betting`, `chat`, `topic_management`, `text_commands`, `squad_ai`, `tracker`, `base` |
| `services/ai/` | `ai_recognizer.py` (match-result Gemini Vision OCR), `squad_recognizer.py` (lineup OCR), `ai_chat.py` («Темшик» persona), `persona_base.py` |
| `services/graphics/` (9 modules) | Pillow renderers: standings tables, club/player/FC cards, club schedules, round digests, top-stats, `division_theme.py`, `player_photos.py` |
| `services/sports/` + `sports_provider.py` | External live-football provider adapters plus `cache`, `circuit`, `limiter`, `freshness`, `health`, `odds_sync` |
| `services/` (root, ~39 modules) | Betting/market engines, ELO, Poisson, risk, settlement, gamification, seasons, `topic_cache.py` |
| `api/` (18 modules) | `aiohttp` Mini App API — `server.py`, `auth.py`, `rate_limiter.py`, and 15 `routes_*.py` modules |
| `web/` | Mini App frontend (static `index.html`, `css/`, `js/` — `api`, `app`, `effects`, `store`, `tg`, `ui`) |
| `utils/` | `media_utils.py`, a thin re-export wrapper over `services/animation_sender.py` |
| `scripts/` (11 scripts) | One-off operational scripts (DB audit, backfills, imports, bulk club binding, cache refresh, season reset) |
| `tests/` | 129 `test_*.py` files, one per feature area; no `__init__.py`, no local `conftest.py` |
| `assets/` | **Not in git** — emptied on 2026-09-18 with the КПЛ season. Runtime recreates `avatars/` and `players/` on demand; `logos/` must be refilled by hand (see below) |
| `reports/` | Historical `PHASE_*.md` plans/matrices/reports, `FIX_0*.md` notes and `*_AUDIT.md` audits, moved off the repo root |
| `tasks/`, `docs/` | Working plan/todo notes and `PURGE_SEASON_GUIDE.md` |
| `.github/workflows/` | `tests.yml` — the pytest CI job |

`handlers/base.py` holds shared helpers, including the role checks described below.
`handlers/squad_ai.py` exists as its own module because `handlers/admin.py` already imports
from `handlers/cabinet.py` and both need it. `handlers/tracker.py` + `api/routes_tracker.py`
serve the Logovo Tracker mobile app: `/tracker` and `/app` issue a single-use 4-digit PIN,
valid 10 minutes, that the app exchanges for a session token.

### Handler registration order matters

`register_all_handlers()` in `handlers/__init__.py` registers in a deliberate sequence:

1. `global_lockdown_guard` as a `TypeHandler` at **group=-1** — runs before everything else.
2. Group-chat tracking at group=1.
3. User handlers → cabinet/admin FSM conversations → betting handlers.
4. **Last:** the catch-all text/voice `MessageHandler` that routes to the AI chat, then a
   catch-all `CallbackQueryHandler`.

Anything registered after the AI catch-all will never fire. Add new handlers *before* it.

---

## Storage rules

- `database.py` owns all SQL. Do not open connections or write queries elsewhere.
- Always go through the `transaction()` context manager. It is **re-entrant**: a nested
  call on the same thread joins the outer transaction and commits only when the outermost
  scope exits. This makes composite operations (e.g. confirm match + advance cup series)
  atomic — rely on it rather than hand-rolling multi-step commits.
- `get_connection()` sets `journal_mode=WAL`, `busy_timeout=10000`, `foreign_keys=ON`,
  and `row_factory = sqlite3.Row`. Rows are accessed by column name.
- **Every** query must be parameterized. The one intentional exception is the migration
  helper that interpolates column names from the hardcoded `SAFE_COLUMNS` tuple; it is
  annotated as such. Do not add new interpolation.
- Schema changes are additive migrations guarded by `CREATE TABLE IF NOT EXISTS` and
  `schema_migrations`. Never rewrite or drop an existing table.
- The DB file is gitignored. `league.db` is local state, never a fixture.

---

## Domain model

**Tournaments** (`tournaments`) have `type IN ('league', 'cup', 'friendly')`. Row id 1 is
seeded as the main league, «Логово Фифарей (Основная Лига)».

**Divisions** (`divisions`) partition a tournament. Each carries a unique `code` and
optional `group_chat_id` + `topic_id`, binding it to one forum topic in one Telegram
group. `services/topic_cache.py` caches this routing and is reloaded during handler
registration — call `topic_cache.reload_cache()` after mutating division topic bindings.

`code` is a lookup key, not a label: `config.DIVISION_CLUBS` and
`services/graphics/division_theme.THEMES` are both keyed by it, so divisions **1–5 must
carry `DIV_1`…`DIV_5`** — otherwise they render with no clubs and the default palette.
`ensure_canonical_divisions()` seeds those rows with `INSERT OR IGNORE`, which cannot
repair a row that already exists, so `repair_canonical_division_codes()` (migration
`012_canonical_division_codes`) rewrites a code only when the current one finds no roster
and the canonical one is free. Codes for new divisions come from
`handlers/admin._division_code_from_name`, which transliterates Cyrillic — dropping it
left «Дивизион 6» with an empty string and a random `DIV_XXXX` that matched nothing.

**League play** runs through `rounds` / `rounds_v` and `matches`. **Cup play** uses
`cup_series` (stage, series number, per-side win counts, winner, status).

**Teams** live in `users.team_name`, one club per coach. Club names are globally unique —
`idx_users_team_name_unique` enforces `UNIQUE(LOWER(TRIM(team_name)))` across all divisions,
so a name identifies a club on its own and name-keyed lookups are safe.

`config.DIVISION_CLUBS` holds the season's roster: a `{division code: [16 club names]}` map
over `DIV_1`…`DIV_5`, 80 clubs in total. It is **seed data, not the participant list** — an
actual participant exists only once a coach registers and lands in `users.team_name`.
Anything that counts or ranks real participants (standings, debts, digests) queries `users`
scoped by `division_id` and must keep doing so. The one place the map is authoritative is
`database.get_division_teams(division_id)`: it unions the seeded roster (keyed by
`divisions.code`) with the division's registered coaches and its scheduled match clubs, and
every admin screen that offers *a club to pick* — «Составы команд», «Изменить клуб», add-player —
goes through it. Without the seed those pickers were circular: a club only appeared once
somebody already owned it, so the first coach of a fresh season could never be bound to one.
The КПЛ-era `config.CLUBS` / `config.KPL_TEAMS` lists are gone.

The **start-of-season placement** is done in bulk by `scripts/bind_clubs_to_players.py`
rather than club by club through the admin screen: its `BINDINGS` table holds
`{division number: {club: username}}`, it validates the whole plan (clubs against the
division's roster, no club or coach twice) before the first write, applies one division per
`transaction()` so a failure rolls that division back whole, and is idempotent. It is
dry-run by default — `--apply` writes. Remember that binding resets warns for both the new
and the previous owner (`set_player_club`), which is right pre-season and wrong mid-season.

Team-name resolution from OCR output goes through `resolve_team_name` and
`detect_teams_from_players`, both backed by **`club_registry.py`** — a pure-CPU module at
the repo root that imports `config` and nothing else. It deliberately sits *below*
`database.py`, which re-exports `resolve_team_name`, `teams_match`, `normalize_team_name`
and `TEAM_ALIASES`, so existing `database.…` call sites keep working. Never import
`database` from it.

Resolution walks EXACT → ALIAS → JOINED → PREFIX → FUZZY and stops at the first
*unambiguous* tier. A tie at any tier ends the walk with no match instead of falling
through to a weaker one. Fuzzy is guarded by `FUZZY_MIN_LEN = 5`,
`FUZZY_THRESHOLD = 0.87` and `FUZZY_MARGIN = 0.07` (the gap to the runner-up). There is no
substring tier: `Расинг` is both a club and a substring of `Расинг Ланс`, so that tier had
no safe version. `resolve_team_name` returns its input unchanged when nothing resolves;
`resolve_team_name_ex` returns `(canonical, method, confidence, candidates)` when the
caller needs to know *how* confident the answer is. Results are memoised —
`reload_registry()` is the only thing that invalidates them.

The canonical list is `config.CLUB_REGISTRY` — a flat slice of `DIVISION_CLUBS`, built at
import. The resolver does not care which division a name belongs to, and names are unique
league-wide anyway, so one flat list is the whole registry. There is deliberately **no
fallback**: an empty `CLUB_REGISTRY` means an empty registry, and every name then resolves
to itself. A club outside the list also resolves to itself, which is safe, but `teams_match`
will not merge an OCR typo of it — a typo cannot be told apart from a genuinely similar club
without knowing the club list. Refusing to merge is recoverable; silently merging two
coaches' clubs is not.

`TEAM_ALIASES` covers all 80 clubs, grouped by division — ~260 keys carrying the short
forms, nicknames and latin transliterations coaches and OCR actually produce (`Ман Сити`,
`МЮ`, `Барса`, `Юве`, `Леверкузен`, `man utd`, `bayern`). Keys are compared *after*
`normalize_team_name`, so write them already normalized: lowercase, `э`/`ё` folded to `е`,
hyphens as spaces. An alias key that is the canonical name of a *different* club is
reported by `get_dropped_aliases()` and ignored; one pointing outside the registry shows up
in `get_orphan_aliases()`. Both must stay empty — there are tests for it.

Forms that fit two live clubs are deliberately absent and must stay absent: `Реал`
(Мадрид/Сосьедад), `Интер` (Милан/Майами), `Манчестер` (Сити/Юнайтед), `Мадрид`
(Реал/Атлетико), `Юнайтед` (МЮ/Ньюкасл), `paris` (Париж/ПСЖ). They correctly return no
match rather than guessing. Also absent on purpose: `байа` (shorter than `FUZZY_MIN_LEN`,
and a test guards that), `фенер` (the PREFIX tier owns it) and `фенербахе` (FUZZY owns it)
— an alias would take the work away from the tier the tests check. Note that the ALIAS tier
runs before FUZZY, which is why `байерн` can safely alias to Бавария despite sitting one
letter from the live club Байер.

`normalize_team_name` folds `ё`, `э`, latin `ë`, `ø` and `ö`, so the variant transliterations
Russian speakers actually type — `Фулхем`, `Вест Хем`, `Тоттенхем`, `Нешвилл`, `Евертон`,
`Кристал Пелас` — hit their club on the EXACT tier instead of missing it (fuzzy only ever
rescued the long ones). The `э` fold is safe because no two clubs in the roster collapse
into one canon under it; `TestTotalityInvariant` is what keeps that true as clubs change.

**Adding a club to the tournament means adding its name to the right division in
`DIVISION_CLUBS`.** `python scripts/audit_team_resolution.py` reports
registry↔`users.team_name` drift, name collisions and clubs that sit too close to the fuzzy
threshold; it is read-only (the connection is closed by a SQLite authorizer) and
`--emit-config` prints a ready block.

**Club logos** are a second, independent step. `TEAM_LOGO_MAP`
(`services/graphics/table_generator.py:16`) maps each club name to a PNG filename and covers
all 80 clubs, grouped division by division; the Pillow renderers read it via
`get_team_logo_filename`. That function falls back to a short substring chain for forms
`resolve_team_name` misses; the chain is order-sensitive (`Спортинг` contains `порт`, so it
is tested before `Порту`).

`assets/logos/` itself was emptied with the КПЛ season and is not in git, so **no file
exists yet** — the map records the agreed filename, and every club still renders with the
blank-badge fallback until the PNGs are dropped in. Every load site is guarded by
`os.path.exists`, so a missing or unmapped logo degrades to an empty badge and never raises.

The Mini App keeps its **own** copy — `TEAM_LOGO_MAP`, `TEAM_LOGO_ALIASES` and
`getTeamLogoUrl` in `web/js/ui.js:9` — because it serves `/assets/logos/…` directly without
touching Pillow. Same 80 filenames, and `TEAM_LOGO_ALIASES` mirrors `club_registry`'s
`TEAM_ALIASES` key for key (alias → canonical club, not → filename, so renaming a logo is
still one edit). Latin keys are derived from the filenames at load, and `normalizeLogoKey`
folds the same characters as `normalize_team_name`. Lookup mirrors the backend tiers EXACT →
ALIAS → JOINED (noise tokens such as `ФК`/`CP` dropped, then tokens glued in both directions
against a joined index whose ambiguous keys are discarded) and stops there. There is **no**
substring pass, for the same reason the resolver has no substring tier: no safe version of it
exists. `Юнайтед` sits inside both Манчестер and Ньюкасл Юнайтед, `порт` inside `Спортинг`,
`paris` inside Paris FC and PSG — an empty badge is recoverable, another club's crest is not.
The JS side has no PREFIX or FUZZY tier, so it is strictly more conservative than the
backend, never bolder; names reaching the Mini App are already canonicalized server-side.

`TestLogoMapCoversTheRoster` in `tests/test_club_card.py` keeps the Python map in step with
`DIVISION_CLUBS`; the JS copy has no such guard, so a roster or alias change means editing
both by hand.

**Discipline:** unplayed matches accrue debts, tracked from `DEBT_TRACKING_START_DATETIME`.
Three job-queue tasks drive it — deadline reminders and the debt lifecycle tracker every
30 min, a debts digest to the ПРЕДЫ thread every 12 h. `MAX_WARNS_LIMIT = 4`.

`register_jobs()` in `main.py` schedules six more beyond those three, each in its own
try/except block: live provider sync (45 s), intelligence cache (5 min), the notification
queue (15 s), bet settlement (60 s), and the round preview / round digest posts to the
АНАЛИТИКА topic (10 / 15 min). Settlement in particular used to run inline on Mini App
requests — keep it off the request path.

**Betting** ("Logovo.bet") is a closed virtual-currency system: `user_wallets`,
`coin_transactions`, `markets`/`market_selections`, `user_bets`/`bet_items`, plus risk,
exposure, cashout and settlement engines. No real money is involved anywhere.

Automatic repricing (`odds_engine.generate_match_markets`) is smoothed by
`smooth_match_repricing`: each model change moves every odd of a match by at most ±15%
(`MAX_REPRICE_STEP`), using one shared blend factor so the line never loses its margin.
`market_selections.model_odds` keeps the raw model price, so re-fetching the line does not
keep stepping toward it. Line tiles (`bet_markets`) take their odds from the smoothed
selections, which placement validates against — never save raw model odds into a tile.

**Gamification** pays out of the same closed economy, so rewards are calibrated against it
rather than against round numbers: the starting wallet is `INITIAL_WALLET_BALANCE` (677 🪙),
the daily bonus 250 🪙, the payout ceiling 10 000 🪙. `seed_gamification_catalog` therefore
bands `reward_coins` by rarity — common ≈150–300, rare ≈500–1 000, epic ≈1 000–1 500,
legendary ≈2 500–5 000, with `reward_xp` at roughly half, because each level gained already
pays 500 🪙 via `add_user_xp`. `tests/test_gamification.py::TestAchievementsCatalog` enforces
the bands and the ≈37 000 🪙 total for 100% completion. The seed is an **upsert and never
deletes**, so retiring an achievement means dropping it from the seed list and setting
`achievements_catalog.is_active = 0` (migration `016`); `get_user_achievements` then hides it
unless the caller already owns it, keeping the `user_achievements` FK alive and the
"N из M" denominator honest.

`user_progression` carries **two** unrelated streaks. `current_streak` / `best_streak` belong
to `StreakEngine.process_bet_outcome` and count consecutive winning bets; `login_streak` /
`best_login_streak` belong to `check_and_update_login_streak` and count consecutive days.
They shared one column until migration `015` and a win streak was read as login days, handing
out `ACH_LOGIN_3` on day one — keep the two engines off each other's columns. A season reset
must clear `last_active_date` along with the login counters, or the first login of the new
season continues the old streak.

---

## Roles and access

Three distinct levels, all resolved in `handlers/base.py` — use these helpers, never
compare against `config.ADMIN_IDS` inline:

- `is_global_admin(telegram_id)` — full access, includes the `ADMIN_IDS` env list.
- `is_admin(telegram_id)` — global admins plus per-division admins (`division_admins`).
- `is_admin_user(user_id)` — the general check used by most handlers.

`config.py` re-reads `config.ADMIN_IDS` dynamically inside these helpers, so admin changes
take effect without a restart. Keep that behaviour.

**Lockdown:** `LOGOVO_LOCKDOWN=true` restricts the bot to global admins via the group=-1
guard. It is the only rollout gate — Mini App access (`api/auth.py::check_user_access`) and
the betting handlers defer to it and nothing else. That check is deliberately FAIL-CLOSED:
any internal error must reject, never allow.

---

## Conventions

- Async throughout. Never block the event loop — SQLite calls are short and synchronous by
  design, but image generation and network I/O must not stall handlers.
- Telegram messages use HTML parse mode, concise keyboards, and Retina 2x/3x Pillow output.
- User-facing strings are Russian; code, identifiers, and docstrings are English. Comments
  in existing files are mixed — match the file you are editing.
- Background jobs register in `register_jobs()` and are wrapped in try/except so one
  failing subsystem cannot take down the others.
- Commits follow Conventional Commits (`feat:`, `fix(web):`, `refactor:`, `chore(agents):`).
- The repository is [`sp1r1tVSA/logovobot`](https://github.com/sp1r1tVSA/logovobot), default
  branch `main`. Work lands **directly on `main`** — do not create feature branches or open
  pull requests unless asked. A session running in a `.claude/worktrees/` worktree still
  pushes its commits to `main` (`git push origin HEAD:main`) rather than leaving them on the
  throwaway `claude/*` branch.
- OCR must stay **perceptual**: Gemini extracts what is visibly on screen — coordinates,
  text, goals, assists — and `team1`/`team2` map to screen left/right. Team identification,
  side assignment, and squad enrichment are done deterministically afterwards in Python and
  SQLite (`detect_teams_from_players`, `match_and_enrich_squad`). Never let the model infer
  squads from the database.

---

## Secrets

`.env` is gitignored and must stay that way. `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`,
`GEMINI_CHAT_API_KEY`, and `SPORTS_API_KEY` must never appear in code, logs, tests, or
commits. `.env.example` documents the variable names only. `league.db` contains real user
data and is likewise never committed.

---

## Related files

- `.agents/AGENTS.md` — the ECC agent framework's project instructions. Its project path is
  now correct, but its module map is still **stale**: it lists `ai_recognizer.py`,
  `ai_chat.py`, and `table_generator.py` at the repo root, but they now live under
  `services/ai/` and `services/graphics/`. It also names a `google-genai` dependency that
  the project does not use. Prefer this file when the two disagree.
- `.claude/prds/logovobot.prd.md` — original product brief. Its milestones still read
  "pending" although the corresponding features shipped; treat it as historical intent,
  not current status.
- `SPEC.md`, `SPEC-team-name-resolution.md` — current specs; the second is the authority on
  the resolver tiers and thresholds described above.
- `reports/` — `PHASE_*.md`, `FIX_0*.md`, `PRODUCTION_AUDIT.md`, `FULL_BOT_AUDIT.md`,
  `BUTTON_AUDIT.md`, `MINIAPP_ROUTE_AUDIT.md`, `Project_Audit_Report.md` and
  `MANUAL_TEST_DIVISION_COMMANDS.md` — per-phase plans, test matrices, fix notes and final
  reports. Useful history for why a subsystem looks the way it does. Moved off the repo
  root on 2026-09-18; only `CLAUDE.md`, `README.md` and the two `SPEC*.md` stay there.
- `.claude/` (agents, commands, skills, prds) and `.apm/` hold agent tooling, not runtime
  code. `.claude/worktrees/` contains throwaway git worktrees.
