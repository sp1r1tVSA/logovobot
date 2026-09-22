# FIX-10A — Legacy NULL `matches.division_id`: единый effective division scope для гейта и RiskEngine

Дата: 2026-09-22 (МСК). Ветка `main`, коммит-основа `37d5259`.
Предшественник: read-only исследование FIX-10 (результат выдан в сессии, отдельного файла в
`reports/` нет) — оно и установило, что соглашение `NULL → Division 1` менять не нужно.

---

## 1. Исходная проблема

`FIX-10` подтвердил: соглашение «у legacy-матча `matches.division_id IS NULL`, и это
дивизион 1» — сознательный legacy compat, а не междивизионная уязвимость: клиент управляет
только `match_id`, весь scope (`season_id` / `division_id` / `round_number`) вычисляется на
сервере из строки матча внутри `_bet_placement_lock` + `transaction()`. Исправлять само
соглашение не требуется (и прямо запрещено: бэкфилл и NOT NULL-миграция исключены).

Но в `place_user_bet()` обнаружилась асимметрия одного и того же правила в двух ветках одного
и того же вызова:

| Шаг | Что получал `division_id` до фикса |
|---|---|
| `evaluate_round_betting_gate` (свой вызов внутри гейта, `database.py:9068`) | `1` — нормализация `... is not None else 1` |
| `evaluate_round_betting_gate` (тот же инвариант в `RiskEngine`, `services/risk_engine.py:267`) | `1` — та же нормализация |
| `RiskEngine.evaluate_bet(..., division_id=div_id)` (`database.py:8953`) | **`None`** — `div_id = m_r["division_id"] if "division_id" in keys else None` |

Линия тура для legacy-матча поэтому проверялась как линия дивизиона 1, а риск-контроль — как
контроль «без дивизиона»:

1. `BettingLimitsService.get_user_effective_limits(user_id, division_id=None)`
   (`services/betting_limits.py:83`) брал `base = get_system_limits()`, а не
   `get_division_limits(1)` → персональные лимиты дивизиона 1 (`max_bet`, `max_payout`,
   `market_exposure_limit`, `division_exposure_limit`, `max_open_bets`) для такого матча не
   применялись вовсе;
2. ветка `8c` в `services/risk_engine.py:459` — `if division_id:` — при `None` не выполнялась,
   то есть **потолок ответственности дивизиона для legacy-матча не работал**.

Отказ не был «междивизионным»: ставки на чужие дивизионы разрешены политикой проекта, и
`rounds`-гейт продолжал работать по Д1. Потерянная часть именно финансовая — потолок ставки
и потолок ответственности дивизиона.

## 2. Точное место исправления

Единственное изменённое место в production-коде — нормализация `div_id` в `place_user_bet()`:

`database.py:8941-8952` (внутри `with _bet_placement_lock, transaction()`, перед вызовом
`RiskEngine.evaluate_bet` на `database.py:8953`):

```python
m_r = cursor.fetchone()
if m_r:
    keys = m_r.keys()
    # Legacy-строка матча с division_id IS NULL — это дивизион 1
    # (соглашение из evaluate_round_betting_gate и из per-selection
    # проверки ниже). Без нормализации RiskEngine получил бы None,
    # взял бы системные лимиты вместо лимитов дивизиона и пропустил
    # ветку division_exposure_limit.
    div_id = m_r["division_id"] if "division_id" in keys and m_r["division_id"] is not None else 1
    risk_ctx_round = m_r["round_number"] if "round_number" in keys else None
    risk_ctx_season = m_r["season_id"] if "season_id" in keys else None
```

Diff: `+6 / -1`, только это место (`git diff --stat`: `database.py | 7 ++++++-`).

Форма выражения скопирована с уже существующего `database.py:9068` (per-selection проверка
гейта в той же функции) — то есть правило переиспользовано как есть, без нового resolver'а, без
новой функции, без переноса вызова `RiskEngine`. `div_id` в функции читается только в двух
местах: аргумент `RiskEngine.evaluate_bet` и лог исключений риск-проверки
(`database.py:8970`) — в обоих он обязан означать «дивизион матча».

Номера строк ниже по `database.py` сдвинулись на `+5` относительно `HEAD` (в `AGENTS.md`
per-selection гейт упомянут как `:9063` — сейчас это `:9068`).

Начальное `div_id = None` (`database.py:8930`) оставлено намеренно: оно означает «строка матча
не прочитана / `match_id` не передан», и в этом случае купон отсекается ранее (валидация
исходов и гейт), так что до риск-движка такой путь не доходит.

## 3. Почему правило `NULL → Division 1` не менялось

- Изменён только **аргумент, передаваемый в RiskEngine**, — не правило разрешения дивизиона.
  Источники истины (`evaluate_round_betting_gate:5970`, per-selection `:9067`,
  `risk_engine.py:267`) остались буквально теми же байтами.
- Схема не тронута: `matches.division_id` по-прежнему `INTEGER DEFAULT NULL`
  (`database.py:624`), legacy-строки не бэкфиллятся, `NOT NULL` не добавлялся, миграций нет.
- Чужой дивизион подстановкой не стал: `NULL` по-прежнему означает ровно `1`, и этот `1`
  используется только для выбора лимитов и расчёта ответственности того же дивизиона, линия
  которого гейт уже проверила. Междивизионная изоляция (`FIX_03`) не затронута.
- Новый resolver не создавался; `get_division_teams` / `resolve_division_target` / RBAC не
  трогались, семантика доступа не менялась (RBAC работает с `users.division_id`, а не с
  `matches.division_id`).
- `NULL` в scopeRiskEngine больше не «system limits» — но `NULL` как **значение в БД**
  осталось валидным legacy-состоянием, и ни один путь не начал его перезаписывать.

## 4. Какие лимиты теперь применяются к legacy-матчу

Для купона, первый матч которого имеет `matches.division_id IS NULL`, `RiskEngine` получает
`division_id=1` и через `get_user_effective_limits(user_id, division_id=1)` берёт
`get_division_limits(1)` → override-значения из `risk_limits_config` (`scope_type='division'`,
`scope_id=1`), а при их отсутствии — системные дефолты:

| Лимит | До фикса (legacy-матч) | После |
|---|---|---|
| `max_bet` | только global/user | override Д1 → global/user |
| `max_payout` | только global/user | override Д1 → global/user |
| `max_open_bets` | только global/user | override Д1 → global/user |
| `market_exposure_limit` | только global | override Д1 → global |
| `division_exposure_limit` | **ветка не выполнялась** | выполняется по `get_division_exposure(1)` |
| `min_bet`, `max_daily_stake`, `max_daily_loss`, `max_open_exposure`, `global_exposure_limit` | без изменений | без изменений |

Иерархия user → division → global (`BettingLimitsService`) не менялась: пользовательский
override по-прежнему ограничивает сильнее дивизионного.

Осталось за скоупом (осознанно, это другое следствие того же legacy-соглашения):
`services/exposure_service.py:122` считает ответственность дивизиона как
`WHERE mat.division_id = ?` **без `COALESCE`**, поэтому ставки, уже принятые на
NULL-матчи, в ответственность Д1 не накапливаются. FIX-10A делает применимым потолок к новой
ставке; накопление legacy-строк потребовало бы бэкфилла, который запрещён.

## 5. Тесты

Новый файл: `tests/test_fix10a_legacy_null_risk_scope.py` (7 тестов, свой temp `league.db` на
тест, `database.py` не мокается, порядок внутри файла фиксирован A→G). Фикстура повторяет стиль
`tests/test_round_betting_isolation.py`: один сезон, туры `5` в дивизионах `1/2/5` с
`is_open=0, bets_open=1`, по матчу на scope (в т. ч. `division_id IS NULL`), реляционные
`markets`/`market_selections` + legacy `bet_markets`; все `INSERT` в таблицы с DEFAULT-timestamp
перечисляют колонку явно через `datetime('now', '+3 hours')`.

| Тест | Проверяет |
|---|---|
| A | Legacy-матч: `RiskEngine` реально получил `division_id=1` (spy на `evaluate_bet`), `max_bet=1000` для Д1 отклоняет ставку 5000 (`MAX_BET_EXCEEDED`), тогда как `division_id=None` даёт `DEFAULT_MAX_BET` |
| B | Матч Д2 → `division_id=2`, работает override Д2 |
| C | Матч Д5 → `division_id=5`, работает override Д5 |
| D | `division_exposure_limit=3000` в Д1: после ставки 1000@2.00 (net 1000) ставка 1200@2.00 на **legacy-матч** отклонена с `DIVISION_EXPOSURE_LIMIT` |
| E | Изоляция лимитов в обе стороны: узкий Д1 не задевает Д2 и наоборот |
| F | Гейт не изменился: `NULL` и явная `1` дают один ответ; закрытая линия Д1 отклоняет, открытая линия Д2 того же тура не «спасает» |
| G | Non-vacuity: тот же купон и сумма при `division_id=None` проходят, при `division_id=1` — `DIVISION_EXPOSURE_LIMIT`; сквозной путь `place_user_bet` отказывает |

Тесты остальных мест, найденных в FIX-10 (23 SQL-`COALESCE`, `_ensure_match_access`,
мёртвый in-chat betting), по требованию скоупа не добавлялись.

Результат: `python -m pytest tests/test_fix10a_legacy_null_risk_scope.py -q` → **7 passed**.

## 6. Non-vacuity (старое значение воспроизведено без правки production-кода)

Одноразовый scratch-скрипт `scratch_fix10a_nonvacuity.py` (создан, выполнен, **удалён**; в
репозитории его нет — `git status` чист) подменял `RiskEngine.evaluate_bet` обёрткой, которая
игнорировала новый аргумент и вычисляла `division_id` ровно так, как это делал код до фикса:
`SELECT division_id FROM matches WHERE id = ?` для первого матча купона, без нормализации.
`database.py` не изменялся ни на байт ради эксперимента.

```
--- CURRENT (fixed) call site ---          ran=7 failures=0 errors=0
--- PRE-FIX value (raw matches.division_id) ---
ran=7 failures=3 errors=0
  FAIL test_a_...: AssertionError: True is not false : Лимит max_bet дивизиона 1 должен отклонить ставку
  FAIL test_d_...: AssertionError: True is not false : division_exposure_limit Д1 должен отклонить ставку на legacy-матч
  FAIL test_g_...: AssertionError: True is not false : С division_id=1 та же ставка должна быть отклонена
  passing: B, C, E, F
```

Падают ровно тесты на лимиты/exposure legacy-матча (A, D, G); B, C, E, F остаются зелёными,
так как поведение матчей с явным `division_id` фикс не меняет — то есть regression-набор
ловит именно эту регрессию и не является «всё падает от всего».

## 7. Прогон тестов

Related-набор (20 файлов: `test_phase9_risk_engine/limits/exposure/atomic_betting`,
`test_risk_engine_fail_closed`, `test_round_betting_cutoff`, `test_round_betting_isolation`,
`test_early_betting_line`, `test_betting_engine`, `test_betting_rules`, `test_open_bets_limit`,
`test_max_active_rounds_limit`, `test_audit_sprint2/3`, `test_production_audit`,
`test_msk_time`, `test_settlement_engine`, `test_betting_api_v2`,
`test_phase5_advanced_betting`, новый файл):

```
2 failed, 202 passed, 58 subtests passed
```

Оба падения — `tests/test_audit_sprint2.py::test_01_max_daily_loss_enforced` и
`::test_02_max_daily_loss_limited_when_partially_spent`.

Полный прогон, рабочее дерево против `HEAD` (тот же часовой window, `--junitxml`, xdist
`-n auto --dist loadfile`):

```
HEAD      : tests=3315 failures=6 errors=0 skipped=7   (3302 passed)
WORKTREE  : tests=3322 failures=7 errors=0 skipped=7   (3308 passed)   Δ tests = +7 — новый файл
```

Два предыдущих полных прогона того же рабочего дерева давали `failures=6`
(3309 passed / 6 failed / 7 skipped).

| Падение | На HEAD | Причина |
|---|---|---|
| `test_audit_sprint2::test_01/02_max_daily_loss_*` | есть | Fixture пишет `created_at = datetime('now')` (UTC), а `risk_engine.py:174` фильтрует `date(created_at) = date('now','+3 hours')` (МСК). В интервале 21:00–23:59 UTC даты расходятся: на момент прогона `date('now')=2026-09-21`, `date('now','+3 hours')=2026-09-22`, поэтому засеянные «потери» не видны и ставка проходит. Часовой window, к FIX-10A отношения не имеет (матч в фикстуре с явным `division_id=1`) |
| `test_sql_schema_consistency[database.py:484]`, `[:490]` | есть | `EXPLAIN` миграционного SQL против временной rebuild-таблицы `round_reminders_v2`, которую allowlist `MIGRATION_TEMP_TABLES` не покрывает (`tests/test_sql_schema_consistency.py:66`). Номера строк указывают в блок миграций `round_reminders`, а не в редактировавшийся `place_user_bet` |
| `test_gamification::test_login_achievement_needs_three_real_days` | есть | Граница календарных суток (МСК-полночь) |
| `test_generated_match_team_names::TestBackfillMigration::test_backfill_repairs_a_schedule_generated_before_the_fix` | есть | Тоже оконный/датовый тест расписания |
| `test_squad_ai_photo_prefetch::test_replace_schedules_photo_prefetch` | **нет** | Тайминг-флейк под нагрузкой 20 xdist-воркеров: в финальном прогоне упал, в трёх изолированных прогонах файла — 3/3 passed, в двух предыдущих полных прогонах — не падал. К ставкам, лимитам и `division_id` отношения не имеет |

Итог по полному прогону: **новых падений фикс не внёл** — 6 детерминированных падений
воспроизведены на `HEAD` в отдельном temp-worktree (`git worktree add --detach … HEAD`, удалён
после прогона; `.claude/worktrees/` не тронуты), седьмое — флейк, независимый от изменения.
Существующие тесты не ослаблялись, порядок нумерованных тестов не менялся, БД не мокалась.

`test_msk_time.py` — passed: новых запрещённых источников времени нет, единственная правка не
содержит SQL; `INSERT` в тестовой фикстуре перечисляют timestamp-колонки явно.

## 8. Изменённые файлы

| Файл | Изменение |
|---|---|
| `database.py` | `+6 / -1` — нормализация `div_id` в `place_user_bet()` (`:8944-8950`) |
| `tests/test_fix10a_legacy_null_risk_scope.py` | новый, 7 regression-тестов A–G |
| `reports/FIX_10A_LEGACY_NULL_RISK_SCOPE.md` | этот отчёт |

Ничего более не изменялось. `scratch_fix10a_nonvacuity.py` и temp-worktree удалены.

## 9. Подтверждение неизменности запретных зон

- **Tracker** (`api/routes_tracker.py`, `handlers/tracker.py`, `/tracker`, `/app`, PIN→session,
  Tracker OCR, `tests/test_tracker_api.py`) — не читался, не анализировался, не изменялся;
 Tracker-шовные участки `api/server.py`, `api/rate_limiter.py`, `config.py`,
  `handlers/__init__.py` не тронуты.
- **LIVE** (`api/routes_live.py`, `api/routes_admin_live.py`, `services/live_ingestion.py`,
  `live_*`-таблицы, `feature_engine`, `odds_movers`, `recommendation_engine`,
  `live_state_machine`) — вне правки: изменённый участок вызывается только при приёме ставки.
- **Settlement** (`services/settlement_engine.py`, `services/market_settler.py`, расчёт,
  `coin_transactions`) — не изменялся; фикс работает до создания купона и не влияет на
  `evaluate_market_selection` или payout-формулы.
- **Wallet** (`get_or_create_wallet`, списание, `user_wallets`) — не изменялся; проверка баланса
  в `RiskEngine` (`:4. User Wallet Balance Check`) осталась прежней.
- **Odds** — коэффициенты читаются теми же запросами; `odds_version`, `odds_at_placement`,
  re-pricing, `services/sports/odds_sync.py` не тронуты.
- **Cutoff** — `evaluate_round_betting_gate` не изменялся (TEST F фиксирует его прежнее
  поведение); `rounds.is_open/bets_open/deadline` трактуются как раньше.
- **RBAC / division scoping** — `resolve_division_target`, `_ensure_division_access`,
  `_ensure_match_access`, `is_division_admin` не тронуты.
- **Schema / миграции** — ни `CREATE/ALTER/DROP`, ни строк в `schema_migrations`; колонка
  `matches.division_id` осталась nullable.
- **Telegram UI и Mini App UI** — ни одного изменения в `handlers/`, `web/`, inline-клавиатурах,
  callback-data, contractualных кодах ответов (`MAX_BET_EXCEEDED`,
  `DIVISION_EXPOSURE_LIMIT` уже использовались путём `err_dict` в `database.py:9024`).
- **Архитектура** `RiskEngine` и `BettingLimitsService` — сигнатуры, порядок проверок и иерархия
  override'ов не менялись; правка затронула только значение аргумента на call site.
- **Legacy-код** не удалялся: `NULL`-строки `matches` и `bet_markets` остались, поддержка
  legacy-схмы в `place_user_bet` (`:9112-9118`) не тронута.

## 10. Вердикт

Legacy-матч (`matches.division_id IS NULL`) получает один и тот же effective scope в гейте и в
риск-контроле: `division_id=1`. Матчи Д1–Д5 сохраняют свой `division_id` (B, C, E). Лимиты
дивизиона и `division_exposure_limit` применяются фактически (A, D, G + non-vacuity).
Междивизионная изоляция не ослаблена (E, F). Пол suite: детерминированные падения совпадают с
`HEAD`, новых нет.

```
FIX-10A COMPLETE
LEGACY NULL → D1 PRESERVED
RISK SCOPE ALIGNED
NO CROSS-DIVISION REGRESSION
FULL TEST SUITE: 3308 PASSED, 7 FAILED, 7 SKIPPED   (6 из 7 — pre-existing на HEAD, 7-й — timing flake; в двух других прогонах: 3309 PASSED, 6 FAILED, 7 SKIPPED)
NO TRACKER CHANGES / NO LIVE CHANGES / NO SETTLEMENT CHANGES
NO WALLET CHANGES / NO ODDS CHANGES / NO SCHEMA CHANGES
```
