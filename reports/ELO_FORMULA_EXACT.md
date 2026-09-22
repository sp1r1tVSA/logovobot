# Exact Production Elo Formula — Logovobot

> **Статус:** READ-ONLY analysis. Никаких изменений в код или БД не вносилось.  
> **База данных:** `C:\Users\Ислам\Desktop\Projects\log\server_league.db`  
> **Дата анализа:** 2026-09-22

---

## 1. Source of Truth

| Компонент | Файл | Функция | Строки |
|---|---|---|---|
| **Константы формулы** | `services/elo_engine.py` | Module-level constants | 19–23 |
| **Расчёт новых рейтингов** | `services/elo_engine.py` | `EloEngine.calculate_new_ratings()` | 91–130 |
| **Применение после матча** | `database.py` | `_apply_elo_after_match()` | 11673–11733 |
| **Идемпотентная запись рейтинга** | `database.py` | `_elo_set_rating()` | 11644–11670 |
| **Вспомогательное чтение команд** | `database.py` | `_elo_team_names()` | 11621–11641 |
| **Чтение рейтинга из БД** | `database.py` | `get_team_elo()` | 11574–11585 |
| **Запись рейтинга в БД** | `database.py` | `update_team_elo()` | 11588–11618 |
| **Схема таблицы рейтингов** | `database.py` | `init_db()` | 1500–1512 |
| **Схема таблицы идемпотентности** | `database.py` | `init_db()` | 1837–1845 |
| **Вызов при подтверждении** | `database.py` | `confirm_and_finalize_match()` | 2983 |
| **Вызов при исправлении счёта** | `database.py` | `admin_set_match_score()` | 3736 |
| **Бэкфилл прошлых матчей** | `scripts/backfill_model_wiring.py` | `main()` | 132 |

---

## 2. Exact Formula

### Константы (services/elo_engine.py, строки 19–23)

```python
DEFAULT_ELO_RATING     = 1500.0
DEFAULT_K_FACTOR       = 24.0
DEFAULT_HOME_ADVANTAGE = 65.0
BASE_DRAW_PROBABILITY  = 0.28   # только для предсказания вероятностей 1X2
DRAW_SIGMA             = 250.0  # только для предсказания вероятностей 1X2
```

### Полная математическая формула (services/elo_engine.py, строки 103–130)

```
1. Скорректированный рейтинг хозяина:
   effective_R1 = R1 + 65.0

2. Разница рейтингов:
   D = effective_R1 - R2 = (R1 + 65.0) - R2

3. Ожидаемый результат хозяина:
   E1 = 1 / (1 + 10^(-D / 400))

4. Ожидаемый результат гостя:
   E2 = 1 - E1

5. Фактический результат:
   Победа хозяина:  S1 = 1.0,  S2 = 0.0   [score1 > score2]
   Ничья:           S1 = 0.5,  S2 = 0.5   [score1 == score2]
   Победа гостя:    S1 = 0.0,  S2 = 1.0   [score1 < score2]

6. Множитель маржи победы (Margin-of-Victory):
   |dG| = |score1 - score2|
   Если |dG| <= 1:  MoV = 1.0
   Если |dG| = 2:   MoV = 1.35
   Если |dG| >= 3:  MoV = 1.5 + (|dG| - 3) / 8.0
   (Примеры: 3-гол -> 1.5; 4-гол -> 1.625; 5-гол -> 1.75; 11-гол -> 2.5)

7. Изменение рейтинга (дельта):
   delta1 = K x MoV x (S1 - E1)
   delta2 = K x MoV x (S2 - E2)
   где K = 24.0

8. Новые рейтинги (с ограничением):
   R1_new = clamp(R1 + delta1,  800.0, 2400.0)
   R2_new = clamp(R2 + delta2,  800.0, 2400.0)

9. Округление:
   R1_new = round(R1_new, 2)
   R2_new = round(R2_new, 2)
   delta1 (сохраняемая в elo_applied_matches) = round(R1_new - R1, 2)
   delta2 (сохраняемая в elo_applied_matches) = round(R2_new - R2, 2)
   ВНИМАНИЕ: дельта рассчитывается ПОСЛЕ округления новых рейтингов
```

> **Ключевое:** `_apply_elo_after_match` вызывает `EloEngine.calculate_new_ratings(r1, r2, p1_score, p2_score)` **без передачи `home_advantage`** — используется значение по умолчанию `DEFAULT_HOME_ADVANTAGE = 65.0`. Подтверждено строкой 11710 `database.py`.

---

## 3. Parameters

| Параметр | Значение | Источник |
|---|---|---|
| **K-factor** | `24.0` | `elo_engine.py:20`, `DEFAULT_K_FACTOR` |
| **Начальный рейтинг** | `1500.0` | `elo_engine.py:19`, `DEFAULT_ELO_RATING`; `database.py:1505` |
| **Home advantage** | `65.0` | `elo_engine.py:21`, `DEFAULT_HOME_ADVANTAGE` |
| **Divisor** | `400` | `elo_engine.py:105`, `10 ** (-rating_diff / 400.0)` |
| **Основание логарифма** | `10` | `elo_engine.py:105` |
| **Нижняя граница рейтинга** | `800.0` | `elo_engine.py:127` |
| **Верхняя граница рейтинга** | `2400.0` | `elo_engine.py:128` |
| **Точность округления** | `2 знака после запятой` | `elo_engine.py:130`, `round(..., 2)` |
| **Тип данных** | `float` (Python) / `REAL` (SQLite) | `database.py:1505` |
| **MoV 1-гол разница** | `1.0` | `elo_engine.py:118` |
| **MoV 2-гол разница** | `1.35` | `elo_engine.py:120` |
| **MoV 3+ гол разница** | `1.5 + (dG - 3) / 8.0` | `elo_engine.py:122` |
| **Division-specific коэффициент** | **Нет** | — |
| **Season-specific коэффициент** | **Нет** | — |
| **Форма команды** | **Нет** | — |
| **Дополнительные множители** | **Нет** (только MoV) | — |

---

## 4. Match Result Mapping

| Результат | S1 (хозяин) | S2 (гость) | Условие кода |
|---|---|---|---|
| **Победа хозяина** | `1.0` | `0.0` | `score1 > score2` |
| **Ничья** | `0.5` | `0.5` | `score1 == score2` |
| **Победа гостя** | `0.0` | `1.0` | `score1 < score2` |

Источник: `elo_engine.py:108–113`.

**Технические результаты (ТП/ТН):** полностью пропускаются. Проверка `is_technical` в `_apply_elo_after_match`, строки 11690–11691:
```python
if info["is_technical"]:
    return False
```
Рейтинг не меняется, запись в `elo_applied_matches` не создаётся.

---

## 5. Rating Update Flow

```
CONFIRMED MATCH
  confirm_and_finalize_match(match_id, p1_score, p2_score, ...)
  database.py:2918

    [внутри with transaction():]
    UPDATE matches SET status='confirmed', player1_score=?, player2_score=? ...
    database.py:2966-2970

    settle_match_bets(match_id, p1_score, p2_score)
    database.py:2977

    [вне основной транзакции — отдельный блок try/except:]
    _apply_elo_after_match(match_id, p1_score, p2_score)
    database.py:2983
      |
      with transaction() as conn:   <-- отдельная транзакция
        cursor = conn.cursor()
        |
        info = _elo_team_names(cursor, match_id)
        database.py:11621
          SELECT player1_team, player2_team, division_id, season_id, is_technical
          FROM matches WHERE id = ?
          -> если нет строки: return None -> return False
          -> если нет team_name: return None -> return False
        |
        if info["is_technical"]: return False   <-- STOP для ТП/ТН
        |
        team1, team2 = info["team1"], info["team2"]
        division_id, season_id = info["division_id"], info["season_id"]
        |
        SELECT delta1, delta2 FROM elo_applied_matches WHERE match_id = ?
        -> prior = (прошлая запись, если есть)
        |
        r1 = get_team_elo(team1, division_id, season_id)
        database.py:11574
          SELECT elo_rating FROM team_ratings
          WHERE LOWER(team_name)=LOWER(?) AND division_id=? AND season_id=?
          -> если нет строки: return 1500.0
        |
        r2 = get_team_elo(team2, division_id, season_id)
        |
        [если prior существует — ОТКАТ прошлых дельт:]
        r1 -= float(prior["delta1"])
        r2 -= float(prior["delta2"])
        database.py:11707-11708
        |
        new_r1, new_r2 = EloEngine.calculate_new_ratings(r1, r2, p1_score, p2_score)
        elo_engine.py:91
          effective_r1 = r1 + 65.0
          D = effective_r1 - r2
          E1 = 1 / (1 + 10^(-D/400))
          E2 = 1 - E1
          [S1, S2 = f(score1, score2)]
          MoV = f(|score1 - score2|)
          delta1 = 24 * MoV * (S1 - E1)
          delta2 = 24 * MoV * (S2 - E2)
          new_r1 = round(clamp(r1+delta1, 800, 2400), 2)
          new_r2 = round(clamp(r2+delta2, 800, 2400), 2)
          return new_r1, new_r2
        |
        delta1 = round(new_r1 - r1, 2)
        delta2 = round(new_r2 - r2, 2)
        database.py:11711-11712
        |
        bump = (prior is None)
        _elo_set_rating(cursor, team1, div, season, new_r1, bump)
        database.py:11644
          UPSERT team_ratings:
            если строка есть: UPDATE elo_rating=new_r1, matches_counted += (1 if bump else 0)
            если нет: INSERT с matches_counted = (1 if bump else 0)
        |
        _elo_set_rating(cursor, team2, div, season, new_r2, bump)
        |
        INSERT INTO elo_applied_matches (match_id, team1, team2, delta1, delta2, applied_at)
        VALUES (...)
        ON CONFLICT(match_id) DO UPDATE SET
            team1=excluded.team1, team2=excluded.team2,
            delta1=excluded.delta1, delta2=excluded.delta2,
            applied_at=datetime('now','+3 hours')
        database.py:11718-11727
        |
      return True

    resolve_ai_predictions(match_id, p1_score, p2_score)
    database.py:2987


ADMIN SCORE CORRECTION
  admin_set_match_score(match_id, player1_score, player2_score, ...)
  database.py:3736
  -> _apply_elo_after_match(match_id, player1_score, player2_score)
     (тот же путь: откатит старые дельты, применит новые)
```

---

## 6. Idempotency

**Механизм:** таблица `elo_applied_matches` (`database.py:1837–1845`)

```sql
CREATE TABLE IF NOT EXISTS elo_applied_matches (
    match_id   INTEGER PRIMARY KEY,   -- PK гарантирует уникальность по матчу
    team1      TEXT,
    team2      TEXT,
    delta1     REAL NOT NULL DEFAULT 0,
    delta2     REAL NOT NULL DEFAULT 0,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**Логика идемпотентности:**

| Ситуация | Поведение |
|---|---|
| Первое применение (prior=None) | Создаётся запись, `bump=True`, `matches_counted += 1` |
| Повторное применение с тем же счётом | Откатывает дельты, пересчитывает (тот же результат), обновляет запись, `bump=False` |
| Исправление счёта admin | Откатывает старые дельты, применяет новые, обновляет запись, `bump=False` |
| Технический матч | `return False`, ничего не записывается |
| Нет team_name | `return False` |

**Критический баг:** `_apply_elo_after_match` **не проверяет** `status = 'confirmed'`.  
Функция применит Elo к любому матчу с командами и `is_technical=0`, независимо от статуса (`pending`, `scheduled`, `in_progress`).

---

## 7. Match 5326

### Фактические данные из БД

| Поле | Значение |
|---|---|
| `status` | `pending` (сейчас) |
| `player1_score` | `None` |
| `player2_score` | `None` |
| `player1_team` | `Торино` |
| `player2_team` | `Монако` |
| `division_id / season_id` | `2 / 2` |
| `is_technical` | `0` |
| `played_at` | `2026-09-21 12:08:30` |
| `elo_applied_matches.delta1` | `+12.26` (Торино) |
| `elo_applied_matches.delta2` | `-12.26` (Монако) |
| `elo_applied_matches.applied_at` | `2026-09-21 12:08:30` |

### Причина появления записи

Матч 5326 был **временно переведён в статус `confirmed` с каким-то счётом**, Elo применился, затем статус сброшен в `pending` (или счёт обнулён), но запись в `elo_applied_matches` осталась.

**Почему это возможно:**
1. `_apply_elo_after_match` не проверяет `status` — только команды и `is_technical`.
2. Между `confirm_and_finalize_match` (основная транзакция, строка 2926) и `_apply_elo_after_match` (отдельная транзакция, строка 2983) нет атомарности.
3. Если основная транзакция откатилась ПОСЛЕ фиксации Elo — запись в `elo_applied_matches` выжила.
4. Либо: `backfill_model_wiring.py --apply` был запущен, когда матч был `confirmed`, затем статус сброшен административно.

### Математическое воспроизведение delta1=12.26

**Elo-история Торино (div=2, s=2):**
```
Матч 5102 (Торино vs Аякс, 2:2 ничья):   delta1=-2.54  applied 11:33:29
Матч 5326 (Торино vs Монако, score=None): delta1=+12.26 applied 12:08:30  <- БАГ
Матч 5107 (Монако vs Торино, 2:4):        delta2=+18.87 applied 13:01:12
```

**Ело-история Монако (div=2, s=2):**
```
Матч 5106 (Спортинг vs Монако, 2:3):      delta2=+14.86 applied 20:43 (20-09)
Матч 5326 (Торино vs Монако, score=None): delta2=-12.26 applied 12:08:30  <- БАГ
Матч 5107 (Монако vs Торино, 2:4):        delta1=-18.87 applied 13:01:12
Матч 5124 (Монако vs Ривер Плейт, 5:3):   delta1=+12.72 applied 20:25:59
Матч 5115 (Вулверхэмптон vs Монако, 1:1): delta2=+1.54  applied 21:57:51
```

**Обратный инжиниринг при MoV=1.0 (1-гол победа):**
```
delta1 = 12.26 = 24.0 x 1.0 x (1.0 - E1)
  => 1.0 - E1 = 0.5108
  => E1 = 0.4892
  => D = -400 x log10(1/0.4892 - 1) = -7.53
  => effective_R1 - R2 = -7.53
  => R1 - R2 = -7.53 - 65 = -72.53
  (Торино был слабее Монако на ~72.5 пунктов)
```

**При MoV=1.35 (2-гол победа):**
```
delta1 = 12.26 = 24.0 x 1.35 x (1.0 - E1)
  => 1.0 - E1 = 0.3784
  => E1 = 0.6216
  => R1 - R2 = +21.2
  (Торино был сильнее Монако на ~21.2 пунктов)
```

Точный счёт невосстановим без логов, т.к. `player1_score = None` в БД.

### Итог по 5326

Запись в `elo_applied_matches` для матча 5326 — **артефакт бага**. Elo применён к матчу без финального счёта. Рейтинги Торино и Монако содержат «фантомные» дельты, которые повлияли на все последующие матчи этих команд.

---

## 8. Match 5830

### Фактические данные из БД

| Поле | Значение |
|---|---|
| `status` | `confirmed` |
| `player1_score` | `1` (Байя) |
| `player2_score` | `3` (Атлетик Бильбао) |
| `player1_team` | `Байя` |
| `player2_team` | `Атлетик Бильбао` |
| `division_id / season_id` | `4 / 2` |
| `is_technical` | **`1`** |
| `technical_type` | `tp_away` |
| Запись в `elo_applied_matches` | **отсутствует** |

### Почему Elo не применён — корректное поведение

Матч 5830 технический (`is_technical=1`, `tp_away`). Функция `_apply_elo_after_match` проверяет:
```python
if info["is_technical"]:
    return False   # database.py:11690-11691
```
Функция завершается немедленно. Никаких изменений в `team_ratings` и `elo_applied_matches` не происходит.

### Что должно было произойти

**Ничего.** Технические результаты — административные вердикты, не сыгранные матчи. Отсутствие записи в `elo_applied_matches` — **правильно**.

### Гипотетический расчёт (если бы матч не был техническим)

```
Текущие рейтинги (div=4, season=2):
  Байя:            1526.52  (matches_counted=2)
  Атлетик Бильбао: 1526.33  (matches_counted=2)

  effective_R1 = 1526.52 + 65 = 1591.52
  D = 1591.52 - 1526.33 = 65.19
  E1 = 1 / (1 + 10^(-65.19/400)) = 0.5937
  E2 = 0.4063

  Счёт: Байя 1:3 Атлетик Бильбао -> S1=0.0, S2=1.0
  |dG| = 2 -> MoV = 1.35

  delta1 = 24 x 1.35 x (0.0 - 0.5937) = -19.20  (Байя)
  delta2 = 24 x 1.35 x (1.0 - 0.4063) = +19.20  (Атлетик Бильбао)

  Байя:            1526.52 - 19.20 = 1507.32
  Атлетик Бильбао: 1526.33 + 19.20 = 1545.53
```

**ЭТИ ДЕЛЬТЫ НЕ ПРИМЕНЯЮТСЯ** — матч технический, система работает корректно.

---

## 9. team_ratings

### Схема таблицы (database.py:1500–1512)

```sql
CREATE TABLE IF NOT EXISTS team_ratings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name       TEXT NOT NULL,
    division_id     INTEGER NOT NULL DEFAULT 1,
    season_id       INTEGER NOT NULL DEFAULT 1,
    elo_rating      REAL NOT NULL DEFAULT 1500.0,
    matches_counted INTEGER NOT NULL DEFAULT 0,
    last_updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
    UNIQUE(team_name, division_id, season_id),
    FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE,
    FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE CASCADE
)
```

### Правила создания и обновления

| Правило | Описание |
|---|---|
| **Откуда 1500** | Жёстко задано в двух местах: `elo_engine.py:19` (`DEFAULT_ELO_RATING = 1500.0`) и `database.py:11585` (`return 1500.0 if no row`) и `database.py:1505` (DEFAULT в схеме) |
| **Создание строки** | Лениво — строка создаётся первым вызовом `_elo_set_rating` или `update_team_elo` |
| **Инициализация заранее** | Нет — `team_ratings` не заполняется при создании матча, команды или начале сезона |
| **Когда создаётся рейтинг** | При первом подтверждении матча команды через `_apply_elo_after_match` → `_elo_set_rating` |
| **Если нет строки** | `get_team_elo` возвращает `1500.0` → вычисляется как "новичок 1500" |
| **Scope** | `(team_name, division_id, season_id)` — независимо для каждого дивизиона и сезона |
| **matches_counted при первом** | `bump=True` → `matches_counted = 1` |
| **matches_counted при правке** | `bump=False` → `matches_counted` не меняется |
| **Preseason seeds** | Это НЕ Elo рейтинги. `services/preseason_seeds.py` — сила тренера 0.0–1.0 для подбора матчей тура. Не влияет на `team_ratings.elo_rating`. |

### Рейтинг при повторном расчёте

Механизм отката: `r1 -= prior["delta1"]` восстанавливает пре-матчевый рейтинг **математически** — без снапшота. Это значит, что если промежуточные данные потеряны (как в случае match 5326), точное восстановление невозможно.

---

## 10. Recalculation Safety

### Безопасное повторное применение одного матча

```python
database._apply_elo_after_match(match_id, new_score1, new_score2)
```
- Откатывает старые дельты автоматически
- Применяет новые дельты
- Обновляет `elo_applied_matches` через `ON CONFLICT DO UPDATE`
- **Но:** рейтинги всех последующих матчей остаются некорректными

### Полный пересброс (если нужна чистая история)

1. Удалить все строки из `elo_applied_matches`
2. Сбросить `team_ratings.elo_rating = 1500.0`, `matches_counted = 0`
3. Запустить `python scripts/backfill_model_wiring.py --apply` — обработает все `confirmed`, `is_technical=0` матчи в порядке `played_at ASC, id ASC`

### Ловушка матча 5326

Пока запись в `elo_applied_matches` для 5326 существует:
- Любой вызов `_apply_elo_after_match(5326, ...)` с реальным счётом откатит фантомные дельты и применит корректные
- Если вызвать без счёта — функция получит `score=None` и упадёт
- `backfill_model_wiring.py` пропускает матчи без `player1_score IS NOT NULL` (строка 96) — не трогает 5326

### Хронологический порядок критичен

Из `backfill_model_wiring.py:18`:
> "Elo — путь, а не множество, и порядок обработки меняет результат."

```sql
ORDER BY (m.played_at IS NULL) ASC, m.played_at ASC, m.id ASC
```

---

## 11. Exact SQL/Code Inputs Needed

### Для диагностики текущего состояния (READ-ONLY)

```sql
-- Все phantom-записи: Elo применён к non-confirmed матчам или матчам без счёта
SELECT e.match_id, e.team1, e.team2, e.delta1, e.delta2, e.applied_at,
       m.status, m.player1_score, m.player2_score, m.is_technical
FROM elo_applied_matches e
JOIN matches m ON m.id = e.match_id
WHERE m.status != 'confirmed'
   OR m.player1_score IS NULL
   OR m.is_technical = 1;

-- Confirmed non-technical матчи без Elo (пропущенные)
SELECT m.id, m.player1_team, m.player2_team, m.player1_score, m.player2_score,
       m.played_at, m.division_id, m.season_id
FROM matches m
WHERE m.status = 'confirmed'
  AND m.player1_score IS NOT NULL
  AND m.player2_score IS NOT NULL
  AND m.is_technical = 0
  AND NOT EXISTS (SELECT 1 FROM elo_applied_matches e WHERE e.match_id = m.id)
ORDER BY m.played_at, m.id;

-- Все Elo-применения для Торино и Монако в правильном порядке
SELECT e.match_id, e.team1, e.team2, e.delta1, e.delta2, e.applied_at,
       m.player1_score, m.player2_score, m.status
FROM elo_applied_matches e
JOIN matches m ON m.id = e.match_id
WHERE (e.team1 IN ('Торино','Монако') OR e.team2 IN ('Торино','Монако'))
  AND m.division_id = 2
ORDER BY e.applied_at;
```

### Что нужно для точного воспроизведения delta1=12.26 (матч 5326)

| Нужно | Статус |
|---|---|
| Точный счёт матча на момент `2026-09-21 12:08:30` | **ОТСУТСТВУЕТ** в БД (`player1_score=None`) |
| Рейтинги Торино и Монако до 12:08:30 | **ОТСУТСТВУЕТ** (нет audit trail в `team_ratings`) |
| Telegram bot logs / message history | Возможный источник счёта |

**Вывод:** точная математическая воспроизводимость delta1=12.26 **невозможна ретроактивно** без логов бота на момент применения. Запись в `elo_applied_matches` является единственным следом несохранённого события.

---

## Краткая справочная карточка

| Параметр | Значение |
|---|---|
| K-factor | `24.0` (константа, не зависит от дивизиона/сезона/числа матчей) |
| Начальный рейтинг | `1500.0` |
| Divisor | `400` |
| Home advantage | `+65.0` к effective рейтингу хозяина |
| MoV модификатор | Да: 1-гол=1.0, 2-гол=1.35, 3+гол=1.5+(dG-3)/8 |
| Division-specific | Нет |
| Season-specific | Нет |
| Форма команды | Нет |
| Дополнительные множители | Нет (только MoV) |
| Округление | `round(..., 2)` |
| Нижняя/верхняя граница | 800 / 2400 |
| Защита от повтора | `elo_applied_matches` (PK=match_id) + откат дельт |
| Технические матчи | Пропускаются (`is_technical=1 -> return False`) |
| Проверка `confirmed`? | **НЕТ** — баг в системе |
| Match 5326 | `pending` + `score=NULL` + запись в `elo_applied_matches` = **фантомное применение** |
| Match 5830 | `confirmed` + `is_technical=1` -> Elo корректно не применён |
