# Logovobot — Agent Instructions & Architecture Guidelines

This repository contains **Logovobot** (Логово Фифарей / ИИ «Темшик») — a high-performance, asynchronous Telegram bot for managing FIFA/FC e-sports championships, cups, match drafts, automated Gemini AI OCR vision processing, SQLite statistics, and graphics generation.

**Project Path:** `C:\Users\Ислам\Desktop\Projects\log\logovobot`  
**Stack:** Python 3.11+, `python-telegram-bot` v21 (async), SQLite (WAL mode, parameterized transactions), Google Gemini AI OCR (`google-genai` / `google-generativeai`), Pillow (Retina 2x/3x graphics rendering), APScheduler / JobQueue.

---

## Core Principles

1. **Agent-First** — Delegate complex domain tasks to specialized roles (`planner`, `python-reviewer`, `database-reviewer`, `security-reviewer`, `tdd-guide`).
2. **Deterministic Data Integrity** — Always use the `transaction()` context manager in `database.py`. All SQL queries MUST be parameterized. Never mutate database records outside verified repository functions.
3. **Pure OCR & Deterministic Enrichment** — Keep AI Vision OCR strictly perceptual (extracting coordinates, text, goals, and assists without hallucinating database squads). Perform team detection, side assignment, and squad enrichment deterministically in Python/SQLite (`detect_teams_from_players`, `match_and_enrich_squad`).
4. **Security-First** — Never commit `.env`, `league.db`, or expose Telegram tokens / Gemini API keys.
5. **High UI/UX Quality** — Telegram messages must use clean HTML formatting, concise keyboards, and stunning Pillow infographics.

---

## Project Architecture & Module Map

| Module | Responsibility & Rules |
|--------|------------------------|
| `main.py` | Bot entrypoint, ApplicationBuilder, Handler registration, `post_init` background jobs setup. |
| `config.py` | Environment variables, Telegram `TOKEN`, `GEMINI_API_KEY`, Admin IDs, chat/topic IDs, DB path. |
| `database.py` | Thread-safe SQLite repository layer, schema migrations, team resolution (`resolve_team_name`, `detect_teams_from_players`), debt tracking, tournament standings. |
| `ai_recognizer.py` | Gemini 2.5 Flash / 1.5 Flash Vision OCR. Strictly maps `team1` to screen left and `team2` to screen right. Handles multi-screenshot matching (timeline + table). |
| `ai_chat.py` | AI assistant «Темшик» persona for chat discussions, регламент, and match predictions. |
| `table_generator.py` | Pillow-based rendering of standings, retina 2x/3x graphics, cards, and top scorers/assisters. |
| `handlers/drafts.py` | Group topic match draft processing, photo debouncing, OCR triggering, squad matching, team/tour auto-detection, admin interactive approval. |
| `handlers/cabinet.py` | Private message player cabinet, match reporting, player registration, stats cards, squad photo upload. |
| `handlers/admin.py` | League & Cup administration, tech defeats/draws, round lifecycle (open/close), deadlines, squad management, broadcast. |
| `handlers/cup.py` | Cup series, playoff brackets, stage progression, series game tracking. |
| `handlers/common.py` | Shared command handlers, help, rules, and global error handlers. |

---

## Engineering Rules & Invariants

### 1. Database & Transactions (`database.py`)
- Always execute SQLite operations inside `with transaction() as conn:` blocks.
- Enable WAL mode (`PRAGMA journal_mode=WAL;`).
- Never perform string concatenation in SQL queries — always use `?` placeholders.
- When matching team names, use `resolve_team_name()` / `teams_match()` / `normalize_team_name()` — they live in `club_registry.py` and are re-exported from `database.py`. The canonical list is `config.CLUB_REGISTRY`; a new club must be added there, otherwise it resolves only to itself. Resolution stops at the first unambiguous tier, so an ambiguous name yields *no* match rather than a guess — call `resolve_team_name_ex()` when the caller needs to know whether the answer was confident.
- **Squad Player Uniqueness & Normalization (Один игрок = одна запись)**:
  - Inside a specific club (`team_name`), one real footballer MUST have exactly one record in `squad_players`, regardless of spelling variations, Unicode forms, letter case, whitespace, hyphens, or diacritics.
  - SQLite constraint: `idx_squad_players_team_norm` UNIQUE(`norm_team_name`, `norm_name`).
  - Normalization: Always normalize player names using `services.player_names.normalize_player_name_key()` (NFKC/NFKD, lowercase, whitespace collapsing, hyphen/quote unification, diacritic stripping, Cyrillic transliteration).
  - Pre-insertion check: Before adding a player, perform a normalized lookup via `find_player_in_squad(player_name, team_name, conn)`. Never create a new `squad_players.id` if the player exists in that club.
  - Non-destructive updates / Upsert: Use upsert or diff-based synchronization (`add_squad`, `replace_squad`, `set_player_position`). Never unconditionally delete and recreate squads (`replace_squad` preserves canonical player IDs and positions).
  - Relation consistency: `match_events.player_name` and `matches.mvp_player` must align with the canonical `player_name` in `squad_players` and be updated on player renames (`rename_player`).
  - Strict Club Isolation: Do NOT merge players across different clubs (e.g. `Rodrigo` in Real Madrid != `Rodrigo` in Atlético).

### 2. Vision OCR & Drafts Pipeline (`ai_recognizer.py` + `handlers/drafts.py`)
- **No Squad Hints in Gemini Prompt**: Gemini Vision must perform pure optical text extraction from screenshots. Do not pass DB squads into the AI prompt to prevent team hallucination.
- **Side Stability**: `team1` is always left side on-screen, `team2` is always right side.
- **Team & Round Detection**: Use `database.detect_teams_from_players()` to match extracted player names against `squad_players` in SQLite, determining left and right clubs and the target round automatically.
- **Preview Non-Mutation**: Previewing a match draft must NEVER insert unrecognized players into `squad_players` automatically.

### 3. Telegram Bot Handlers (`handlers/`)
- Always use asynchronous handlers with `async def` and `await asyncio.to_thread(...)` for CPU-bound or database operations.
- Handle Telegram API rate limits gracefully (`telegram.error.RetryAfter`).
- Escape user input in HTML parse mode using `html.escape()`.

### 4. Git & Commit Workflow
- Commit format: `<type>: <description>` (`feat`, `fix`, `refactor`, `docs`, `chore`, `perf`).
- Remote: `https://github.com/sp1r1tVSA/logovabot.git`.

---

## Workspace Project Isolation

- **Primary Project**: `C:\Users\Ислам\Desktop\Projects\log\logovobot` (Python 3.11+, Telegram Bot, Gemini AI, SQLite).
- Confine all edits, test runs, and context reads strictly to this repository.
- Never modify or expose secrets in `.env` or `league.db`.
