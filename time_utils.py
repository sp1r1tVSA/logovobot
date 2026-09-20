"""Единые часы проекта — московское время (UTC+3).

Проект жил на двух часах сразу: SQLite писал `CURRENT_TIMESTAMP` в UTC,
`datetime.now()` отдавал таймзону сервера (в Docker — тоже UTC), а дедлайны
туров и время матчей админы вводят руками по Москве. В одной и той же колонке
лежали значения из разных зон, поэтому дедлайн «20:00» истекал в 23:00 МСК.

Здесь одни часы для всего: и для того, что мы пишем в базу, и для того, что
сравниваем, и для того, что показываем. Москва живёт в UTC+3 без перехода на
летнее время с 2014 года, поэтому фиксированный сдвиг точен и не требует
tzdata (её нет в python:*-slim и нет в zoneinfo на Windows).

Модуль намеренно зависит только от стандартной библиотеки и лежит ниже
`database.py` — его импортируют все слои, включая сам `database`.
"""

import datetime as _dt

__all__ = [
    "MSK",
    "MSK_LABEL",
    "MSK_OFFSET_HOURS",
    "SQL_NOW",
    "DT_FORMAT",
    "now_msk",
    "now_msk_str",
    "today_msk",
    "today_msk_str",
    "to_msk",
    "fmt_msk",
    "parse_msk",
]

MSK_OFFSET_HOURS = 3
MSK = _dt.timezone(_dt.timedelta(hours=MSK_OFFSET_HOURS), "MSK")

# Подпись для пользовательских строк: «19.09 21:30 МСК».
MSK_LABEL = "МСК"

# Замена `CURRENT_TIMESTAMP` в SQL: то же самое, но по Москве. Подставляется
# в текст запроса как литерал (аргументов не принимает — параметризовать нечего).
SQL_NOW = "datetime('now', '+3 hours')"

# Формат хранения: ровно тот же, что отдаёт CURRENT_TIMESTAMP, поэтому старые
# и новые строки сравниваются лексикографически и парсятся одним кодом.
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# Форматы, в которых время реально встречается в базе и в вводе админов.
_PARSE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M",
)


def now_msk() -> _dt.datetime:
    """Текущий момент по Москве, naive.

    Naive — намеренно: в базе лежат строки без зоны, и aware-значение сломало бы
    любое сравнение с ними («can't compare offset-naive and offset-aware»).
    """
    return _dt.datetime.now(MSK).replace(tzinfo=None)


def now_msk_str(fmt: str = DT_FORMAT) -> str:
    """Текущий момент по Москве строкой — то, что уходит в базу."""
    return now_msk().strftime(fmt)


def today_msk() -> _dt.date:
    """Сегодняшняя дата по Москве.

    Важно для суточных механик (логин-стрик, дневной бонус): по UTC «новый день»
    наступал бы в 03:00 МСК.
    """
    return now_msk().date()


def today_msk_str() -> str:
    return today_msk().isoformat()


def parse_msk(value) -> _dt.datetime | None:
    """Строка или datetime → naive datetime по Москве. Непонятный формат → None.

    Naive-значение считается уже московским: после перехода на единые часы всё,
    что пишет проект, московское. Aware-значение (например, ISO от провайдера
    live-данных с `+00:00`) переводится в Москву.
    """
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        dt = value
    elif isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        dt = None
        for fmt in _PARSE_FORMATS:
            try:
                dt = _dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(MSK).replace(tzinfo=None)
    return dt


def to_msk(value) -> _dt.datetime | None:
    """Синоним `parse_msk` для мест, где читается как «привести к МСК»."""
    return parse_msk(value)


def fmt_msk(value, fmt: str = "%d.%m.%Y %H:%M", fallback: str = "—") -> str:
    """Значение из базы → строка для пользователя. Мусор отдаём как есть."""
    dt = parse_msk(value)
    if dt is None:
        raw = str(value or "").strip()
        return raw or fallback
    return dt.strftime(fmt)
