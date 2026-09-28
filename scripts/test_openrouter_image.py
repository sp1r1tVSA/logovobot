"""
Скрипт для проверки генерации изображений через OpenRouter Unified Image API.

Использование:
    python scripts/test_openrouter_image.py [ВАШ_OPENROUTER_API_KEY]
или просто запустите:
    python scripts/test_openrouter_image.py
и вставьте ключ по запросу в консоли.
"""

import sys
import os
import json
import base64
import time
import urllib.request
import urllib.error

# Пробуем загрузить ключ из .env, если есть
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENROUTER_URL = "https://openrouter.ai/api/v1/images"

MODELS_TO_TEST = [
    ("inclusionai/ming-image-0.1-design", "Бесплатная модель (NovitaAI)"),
    ("recraft/recraft-v4.1-flash", "Платная топовая модель ($0.007 / арт)"),
    ("black-forest-labs/flux.2-klein-4b", "Платная модель FLUX.2 ($0.014 / арт)"),
]

DEFAULT_PROMPT = (
    "Epic European soccer matchday poster for Besiktas JK, majestic black eagle, "
    "black and white club colors, roaring soccer stadium floodlights, European soccer association football, "
    "classic round soccer ball, green grass pitch, dynamic sports media photography, 4k"
)


def test_generation(api_key: str, model_id: str, prompt: str) -> bool:
    print(f"\n" + "=" * 60)
    print(f"Тестирование модели: {model_id}")
    print(f"Промпт: {prompt[:80]}...")
    print(f"Эндпоинт: {OPENROUTER_URL}")
    print("=" * 60)

    payload = {
        "model": model_id,
        "prompt": prompt,
        "n": 1,
    }
    if "ming" not in model_id:
        payload["aspect_ratio"] = "1:1"

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://logovobot.local",
        "X-Title": "Logovobot Image Test",
        "User-Agent": "Logovobot/ImageTester",
    }

    req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers)
    start_t = time.time()

    try:
        print("⏳ Отправка запроса в OpenRouter... Ожидаем ответ...")
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - start_t
            status = resp.status
            raw_data = resp.read().decode("utf-8")

        print(f"✅ Успешный HTTP ответ {status}! Время: {elapsed:.2f} сек.")
        data = json.loads(raw_data)
        items = data.get("data", [])

        if not items:
            print("⚠️ Внимание: ответ API не содержит картинок в поле 'data':")
            print(raw_data[:400])
            return False

        item = items[0]
        img_bytes = None
        ext = "png"

        if item.get("b64_json"):
            print("📦 Картинка получена в формате Base64! Декодируем...")
            img_bytes = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            img_url = item["url"]
            print(f"🌐 Картинка возвращена ссылкой: {img_url}")
            print("⏳ Скачиваем изображение...")
            download_req = urllib.request.Request(img_url, headers={"User-Agent": "Logovobot/ImageTester"})
            with urllib.request.urlopen(download_req, timeout=30) as dl_resp:
                img_bytes = dl_resp.read()
            if ".jpg" in img_url or ".jpeg" in img_url:
                ext = "jpg"

        if img_bytes:
            filename = f"test_result_{model_id.replace('/', '_')}.{ext}"
            filepath = os.path.abspath(filename)
            with open(filepath, "wb") as f:
                f.write(img_bytes)

            size_kb = len(img_bytes) / 1024
            print(f"🎉 Картинка успешно сохранена!")
            print(f"📁 Путь: {filepath}")
            print(f"📊 Размер файла: {size_kb:.1f} KB")

            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(img_bytes))
                print(f"🖼️ Разрешение: {img.size[0]}x{img.size[1]} px, формат: {img.format}")
            except Exception:
                pass
            return True
        else:
            print(f"⚠️ Не найден b64_json или url в элементе data: {item}")
            return False

    except urllib.error.HTTPError as e:
        elapsed = time.time() - start_t
        print(f"❌ Ошибка HTTP {e.code} ({e.reason}) за {elapsed:.2f} сек.")
        try:
            err_body = e.read().decode("utf-8")
            print(f"📄 Тело ошибки OpenRouter:\n{err_body}")
        except Exception:
            pass
        return False
    except Exception as e:
        elapsed = time.time() - start_t
        print(f"❌ Системная ошибка: {e} ({elapsed:.2f} сек.)")
        return False


def main():
    print("=" * 60)
    print("  ПРОВЕРКА ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ OPENROUTER (Logovobot)")
    print("=" * 60)

    # 1. Получение ключа
    api_key = ""
    if len(sys.argv) > 1 and sys.argv[1].startswith("sk-or-"):
        api_key = sys.argv[1].strip()
    elif os.getenv("OPENROUTER_API_KEY"):
        api_key = os.getenv("OPENROUTER_API_KEY").strip()

    if not api_key:
        api_key = input("Введите ваш OPENROUTER_API_KEY (например, sk-or-v1-...): ").strip()

    if not api_key:
        print("❌ Ключ не введен. Завершение работы.")
        return

    masked_key = api_key[:10] + "..." + api_key[-4:]
    print(f"🔑 Используется ключ: {masked_key}")

    # 2. Выбор модели
    print("\nДоступные модели для проверки:")
    for idx, (m_id, desc) in enumerate(MODELS_TO_TEST, 1):
        print(f"  [{idx}] {m_id} — {desc}")
    print(f"  [all] Проверить все по очереди")

    choice = input("\nВыберите номер модели (по умолчанию 1 — бесплатная): ").strip().lower()

    if choice in ("all", "a"):
        for m_id, _ in MODELS_TO_TEST:
            test_generation(api_key, m_id, DEFAULT_PROMPT)
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(MODELS_TO_TEST):
                target_model = MODELS_TO_TEST[idx][0]
            else:
                target_model = MODELS_TO_TEST[0][0]
        except Exception:
            target_model = MODELS_TO_TEST[0][0]

        test_generation(api_key, target_model, DEFAULT_PROMPT)

    print("\n" + "=" * 60)
    print("Проверка завершена!")
    print("=" * 60)


if __name__ == "__main__":
    main()
