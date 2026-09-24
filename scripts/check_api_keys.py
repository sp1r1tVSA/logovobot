"""
scripts/check_api_keys.py

Диагностика API-ключей Google Gemini и OpenRouter:
проверяет доступность, исчерпание лимитов (429 RESOURCE_EXHAUSTED / Quota exceeded),
баланс OpenRouter и статус моделей.

Запуск:
    python scripts/check_api_keys.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config


def _mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def check_gemini_key(api_key: str, model: str = "gemini-3.1-flash-lite") -> tuple[bool, str]:
    base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"
    payload = json.dumps({"contents": [{"parts": [{"text": "Reply 1"}]}]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("candidates"):
                return True, "200 OK (лимит доступен)"
            return True, "200 OK (нет candidates)"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
            err_data = json.loads(body)
            msg = err_data.get("error", {}).get("message", "") or body[:150]
        except Exception:
            msg = body[:150] if body else str(e)
        if e.code == 429:
            return False, f"429 ЛИМИТ ИСЧЕРПАН: {msg.strip()}"
        if e.code == 503:
            return False, f"503 СЕРВЕР ПЕРЕГРУЖЕН: {msg.strip()}"
        if e.code == 404:
            return False, f"404 МОДЕЛЬ НЕДОСТУПНА: {msg.strip()}"
        return False, f"HTTP {e.code}: {msg.strip()}"
    except Exception as e:
        return False, f"Ошибка сети: {type(e).__name__} - {e}"


def check_openrouter(api_key: str) -> None:
    print("\n" + "=" * 60)
    print("🌐 ПРОВЕРКА OPENROUTER (Вкладка ИИ-прогноз)")
    print("=" * 60)
    if not api_key:
        print("⚠️ OPENROUTER_API_KEY не задан в .env")
        return

    print(f"Ключ: {_mask_key(api_key)}")

    # 1. Проверка лимитов и метаданных ключа
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})
            label = data.get("label", "Без названия")
            limit = data.get("limit")
            usage = data.get("usage", 0.0)
            is_free = data.get("is_free_tier", False)
            rate_limit = data.get("rate_limit", {})
            print(f"  • Название ключа: {label}")
            print(f"  • Тариф: {'Free Tier' if is_free else 'Paid'}")
            print(f"  • Использование: ${usage}")
            print(f"  • Лимит расходов: {limit if limit is not None else 'Без ограничений'}")
            if rate_limit:
                print(f"  • Rate limit: {rate_limit.get('requests')} req / {rate_limit.get('interval')}")
    except urllib.error.HTTPError as e:
        print(f"  ❌ Ошибка проверки ключа (HTTP {e.code}): {e.read().decode('utf-8', errors='replace')[:200]}")
    except Exception as e:
        print(f"  ❌ Ошибка соединения с OpenRouter: {e}")

    # 2. Проверка баланса кредитов
    req_credits = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req_credits, timeout=10) as resp:
            cdata = json.loads(resp.read().decode("utf-8")).get("data", {})
            total_credits = cdata.get("total_credits", 0.0)
            total_usage = cdata.get("total_usage", 0.0)
            print(f"  • Доступно кредитов: ${round(total_credits, 4)} (расход за всё время: ${round(total_usage, 4)})")
    except Exception:
        pass

    # 3. Тестовый запрос к настроенным моделям
    models = [m.strip() for m in (config.OPENROUTER_MODEL or "").split(",") if m.strip()]
    print(f"\nТест генерации настроенных моделей:")
    for m in models:
        body = json.dumps({
            "model": m,
            "messages": [{"role": "user", "content": "Reply 1"}],
            "max_tokens": 10,
        }).encode("utf-8")
        req_gen = urllib.request.Request(
            f"https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req_gen, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("choices"):
                    print(f"  • {m}: ✅ Работает (200 OK)")
                else:
                    print(f"  • {m}: ⚠️ Пустой ответ")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_msg = json.loads(err_body).get("error", {}).get("message", err_body[:100])
            except Exception:
                err_msg = err_body[:100]
            if e.code == 429:
                print(f"  • {m}: 🛑 429 ЛИМИТ/КВОТА: {err_msg.strip()}")
            else:
                print(f"  • {m}: ❌ HTTP {e.code}: {err_msg.strip()}")
        except Exception as e:
            print(f"  • {m}: ❌ {type(e).__name__} - {e}")


def main():
    print("=" * 60)
    print("🔍 ДИАГНОСТИКА API-КЛЮЧЕЙ LOGOVOBOT")
    print("=" * 60)

    # ─── 1. Gemini OCR ключи ───
    ocr_keys = config.GEMINI_API_KEYS
    print(f"\n📸 GEMINI VISION OCR (Всего ключей: {len(ocr_keys)})")
    models_to_test = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
    if not ocr_keys:
        print("  ⚠️ GEMINI_API_KEY не задан в .env")
    for i, k in enumerate(ocr_keys, 1):
        print(f"\nКлюч #{i}: {_mask_key(k)}")
        for model in models_to_test:
            ok, msg = check_gemini_key(k, model=model)
            status_icon = "✅" if ok else "❌"
            print(f"  • {model}: {status_icon} {msg}")

    # ─── 2. Gemini Chat ключи ───
    chat_keys = config.GEMINI_CHAT_API_KEYS
    print(f"\n💬 GEMINI CHAT / ТЕМШИК (Всего ключей: {len(chat_keys)})")
    if not chat_keys:
        print("  ⚠️ GEMINI_CHAT_API_KEY не задан в .env")
    for i, k in enumerate(chat_keys, 1):
        print(f"\nКлюч #{i}: {_mask_key(k)}")
        for model in models_to_test:
            ok, msg = check_gemini_key(k, model=model)
            status_icon = "✅" if ok else "❌"
            print(f"  • {model}: {status_icon} {msg}")

    # ─── 3. OpenRouter ───
    check_openrouter(config.OPENROUTER_API_KEY)

    print("\n" + "=" * 60)
    print("🏁 Проверка завершена.")
    print("=" * 60)


if __name__ == "__main__":
    main()
