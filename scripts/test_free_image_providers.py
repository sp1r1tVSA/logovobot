"""
Тест генерации изображений через бесплатные провайдеры.

Провайдеры:
  [1] Hugging Face Inference API + FLUX.1-schnell (РЕКОМЕНДУЕТСЯ)
        — Требует бесплатный HF-токен с huggingface.co/settings/tokens
        — Качество: топ, модель FLUX.1-schnell от Black Forest Labs
        — Лимит: ~$0.10 бесплатных кредитов в месяц

  [2] Pollinations.AI
        — Без ключа и регистрации
        — Качество: среднее, вотермарк

  [3] AI Horde (Stable Horde)
        — Без ключа (анонимный 0000000000)
        — Качество: неплохое (SDXL), но медленно (очередь)
"""

import sys
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error


# ─── HuggingFace Inference API ───────────────────────────────────────────────

HF_MODELS = [
    ("black-forest-labs/FLUX.1-schnell", "FLUX.1-schnell (топ качество, быстро)"),
    ("stabilityai/stable-diffusion-xl-base-1.0", "Stable Diffusion XL (надёжный)"),
    ("Lykon/dreamshaper-xl-1-0", "DreamShaper XL (художественный)"),
]

def test_huggingface(prompt: str, hf_token: str) -> bool:
    print("\n" + "=" * 60)
    print("Провайдер: Hugging Face Inference API")
    print("Тип: Бесплатный токен, ~$0.10/месяц бесплатных кредитов")
    print(f"Промпт: {prompt[:80]}...")
    print("=" * 60)

    for model_id, model_desc in HF_MODELS:
        url = f"https://api-inference.huggingface.co/models/{model_id}"
        payload = {
            "inputs": prompt,
            "parameters": {
                "width": 1024,
                "height": 1024,
                "num_inference_steps": 4,
                "guidance_scale": 0.0,
            }
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {hf_token}",
            "Content-Type": "application/json",
            "x-wait-for-model": "true",
        }
        req = urllib.request.Request(url, data=body, headers=headers)

        print(f"\n⏳ Пробуем модель: {model_id}")
        start_t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                elapsed = time.time() - start_t
                status = resp.status
                img_bytes = resp.read()
                content_type = resp.headers.get("Content-Type", "")

            # Check if it's actually an image
            if img_bytes[:4] in (b'\x89PNG', b'\xff\xd8\xff') or b'PNG' in img_bytes[:8]:
                ext = "jpg" if b'\xff\xd8' in img_bytes[:4] else "png"
                filename = f"test_result_hf_{model_id.split('/')[-1]}.{ext}"
                with open(filename, "wb") as f:
                    f.write(img_bytes)
                size_kb = len(img_bytes) / 1024
                print(f"✅ HTTP {status}, время: {elapsed:.2f} сек, размер: {size_kb:.1f} KB")
                print(f"📁 Сохранено: {os.path.abspath(filename)}")
                return True
            else:
                # Might be a JSON error or loading response
                try:
                    data = json.loads(img_bytes.decode("utf-8"))
                    error_msg = data.get("error", str(data))
                    print(f"⚠️ Ответ JSON (не изображение): {error_msg[:200]}")
                    if "loading" in error_msg.lower() or "estimated_time" in str(data):
                        wait_time = data.get("estimated_time", 20)
                        print(f"⌛ Модель загружается... ожидаем {wait_time:.0f} сек")
                        time.sleep(min(wait_time + 5, 30))
                        continue
                except Exception:
                    print(f"⚠️ Неизвестный ответ: {img_bytes[:200]}")

        except urllib.error.HTTPError as e:
            elapsed = time.time() - start_t
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            print(f"❌ HTTP {e.code} ({e.reason}) за {elapsed:.2f} сек: {err_body[:300]}")
            if e.code in (401, 403):
                print("⛔ Ключ неверный или нет доступа к модели. Проверьте токен.")
                return False
            continue
        except Exception as e:
            elapsed = time.time() - start_t
            print(f"❌ Ошибка: {e} ({elapsed:.2f} сек.)")
            continue

    print("❌ Все HF модели исчерпаны")
    return False


# ─── Pollinations.ai ─────────────────────────────────────────────────────────

def test_pollinations(prompt: str) -> bool:
    print("\n" + "=" * 60)
    print("Провайдер: Pollinations.AI")
    print("Тип: БЕСПЛАТНО, без ключа (вотермарк, среднее качество)")
    print(f"Промпт: {prompt[:80]}...")
    print("=" * 60)

    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&nologo=true&nofeed=true"
    print(f"⏳ GET {url[:90]}...")
    start_t = time.time()

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Logovobot/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - start_t
            status = resp.status
            img_bytes = resp.read()
        print(f"✅ HTTP {status}, время: {elapsed:.2f} сек, размер: {len(img_bytes)/1024:.1f} KB")
        filename = "test_result_pollinations.jpg"
        with open(filename, "wb") as f:
            f.write(img_bytes)
        print(f"📁 Сохранено: {os.path.abspath(filename)}")
        return True
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start_t
        print(f"❌ HTTP {e.code} ({e.reason}) за {elapsed:.2f} сек.")
        try:
            print(f"📄 Тело ошибки: {e.read().decode('utf-8')[:300]}")
        except Exception:
            pass
        return False
    except Exception as e:
        elapsed = time.time() - start_t
        print(f"❌ Ошибка: {e} ({elapsed:.2f} сек.)")
        return False


# ─── AI Horde ────────────────────────────────────────────────────────────────

def test_aihorde(prompt: str, api_key: str = "0000000000") -> bool:
    print("\n" + "=" * 60)
    print("Провайдер: AI Horde (Stable Horde)")
    print("Тип: БЕСПЛАТНО (медленно, волонтёрские GPU, 1-3 мин)")
    print(f"Промпт: {prompt[:80]}...")
    print("=" * 60)

    submit_url = "https://stablehorde.net/api/v2/generate/async"
    payload = {
        "prompt": prompt,
        "params": {
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "sampler_name": "k_euler_a",
            "n": 1,
        },
        "models": ["Stable Diffusion XL"],
        "nsfw": False,
        "trusted_workers": False,
        "slow_workers": True,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "apikey": api_key,
        "Client-Agent": "Logovobot:1.0:t.me/logovobot",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(submit_url, data=body, headers=headers)

    try:
        print("⏳ Отправка задачи в AI Horde...")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        job_id = data.get("id")
        print(f"✅ Задача принята. Job ID: {job_id}")
    except urllib.error.HTTPError as e:
        print(f"❌ Ошибка отправки HTTP {e.code}: {e.read().decode()[:300]}")
        return False
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

    check_url = f"https://stablehorde.net/api/v2/generate/check/{job_id}"
    status_url = f"https://stablehorde.net/api/v2/generate/status/{job_id}"
    start_t = time.time()
    timeout = 180

    print("⏳ Ожидаем очередь AI Horde (может занять 1-3 минуты)...")
    while time.time() - start_t < timeout:
        time.sleep(10)
        try:
            check_req = urllib.request.Request(check_url, headers={"apikey": api_key, "Client-Agent": "Logovobot:1.0"})
            with urllib.request.urlopen(check_req, timeout=10) as r:
                check_data = json.loads(r.read().decode())
            is_done = check_data.get("done", False)
            wait = check_data.get("wait_time", "?")
            queue = check_data.get("queue_position", "?")
            print(f"   Статус: done={is_done}, wait_time={wait}s, queue={queue} | прошло {int(time.time()-start_t)}s")
            if is_done:
                break
        except Exception as e:
            print(f"   Ошибка проверки: {e}")
    else:
        print(f"❌ Таймаут {timeout}s: AI Horde не ответил")
        return False

    try:
        status_req = urllib.request.Request(status_url, headers={"apikey": api_key, "Client-Agent": "Logovobot:1.0"})
        with urllib.request.urlopen(status_req, timeout=10) as r:
            status_data = json.loads(r.read().decode())
        generations = status_data.get("generations", [])
        if not generations:
            print(f"❌ Нет generations: {status_data}")
            return False
        img_url = generations[0].get("img")
        print(f"🌐 URL картинки: {img_url}")

        img_req = urllib.request.Request(img_url, headers={"User-Agent": "Logovobot/1.0"})
        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
            img_bytes = img_resp.read()

        elapsed = time.time() - start_t
        filename = "test_result_aihorde.webp"
        with open(filename, "wb") as f:
            f.write(img_bytes)
        print(f"✅ Сохранено за {elapsed:.1f} сек, размер: {len(img_bytes)/1024:.1f} KB")
        print(f"📁 Путь: {os.path.abspath(filename)}")
        return True
    except Exception as e:
        print(f"❌ Ошибка получения результата: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ТЕСТ БЕСПЛАТНЫХ ПРОВАЙДЕРОВ ГЕНЕРАЦИИ КАРТИНОК (Logovobot)")
    print("=" * 60)

    PROVIDERS = [
        ("1", "Hugging Face + FLUX.1-schnell (РЕКОМЕНДУЕТСЯ, нужен бесплатный токен)"),
        ("2", "Pollinations.AI (без ключа, среднее качество, вотермарк)"),
        ("3", "AI Horde / Stable Horde (без ключа, медленно)"),
        ("all", "Проверить все"),
    ]

    print("\nДоступные провайдеры:")
    for code, desc in PROVIDERS:
        print(f"  [{code}] {desc}")
    print()
    print("  Получить HF токен: https://huggingface.co/settings/tokens")
    print("  Создать аккаунт: https://huggingface.co/join (бесплатно, без карты)")

    choice = input("\nВыберите провайдер (по умолчанию 1): ").strip().lower() or "1"

    prompt = (
        "Epic European soccer matchday poster, Besiktas JK, majestic black eagle, "
        "black and white club colors, roaring stadium with floodlights, green soccer pitch, "
        "dynamic dramatic sports poster art, cinematic lighting, ultra-detailed, 4k"
    )

    hf_token = ""
    if choice in ("1", "all"):
        hf_token = input("Введите HF токен (hf_...): ").strip()
        if not hf_token:
            print("⚠️ Токен не введён — пропускаем Hugging Face")
        else:
            test_huggingface(prompt, hf_token)

    if choice == "2" or (choice == "all" and not hf_token):
        test_pollinations(prompt)
    elif choice == "2":
        test_pollinations(prompt)

    if choice == "3" or choice == "all":
        horde_key = input("\nAI Horde ключ (Enter для анонимного): ").strip() or "0000000000"
        test_aihorde(prompt, api_key=horde_key)

    print("\n" + "=" * 60)
    print("Тест завершен!")
    print("=" * 60)


if __name__ == "__main__":
    main()


import sys
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error


# ─── Pollinations.ai ─────────────────────────────────────────────────────────

def test_pollinations(prompt: str) -> bool:
    print("\n" + "=" * 60)
    print("Провайдер: Pollinations.AI")
    print("Тип: БЕСПЛАТНО, без ключа")
    print(f"Промпт: {prompt[:80]}...")
    print("=" * 60)

    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&nologo=true&nofeed=true"
    print(f"⏳ GET {url[:90]}...")
    start_t = time.time()

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Logovobot/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - start_t
            status = resp.status
            img_bytes = resp.read()
        print(f"✅ HTTP {status}, время: {elapsed:.2f} сек, размер: {len(img_bytes)/1024:.1f} KB")
        filename = "test_result_pollinations.jpg"
        with open(filename, "wb") as f:
            f.write(img_bytes)
        print(f"📁 Сохранено: {os.path.abspath(filename)}")
        return True
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start_t
        print(f"❌ HTTP {e.code} ({e.reason}) за {elapsed:.2f} сек.")
        try:
            print(f"📄 Тело ошибки: {e.read().decode('utf-8')[:300]}")
        except Exception:
            pass
        return False
    except Exception as e:
        elapsed = time.time() - start_t
        print(f"❌ Ошибка: {e} ({elapsed:.2f} сек.)")
        return False


# ─── AI Horde ────────────────────────────────────────────────────────────────

def test_aihorde(prompt: str, api_key: str = "0000000000") -> bool:
    print("\n" + "=" * 60)
    print("Провайдер: AI Horde (Stable Horde)")
    print("Тип: БЕСПЛАТНО, без регистрации (анонимный ключ 0000000000)")
    print(f"API Key: {api_key}")
    print(f"Промпт: {prompt[:80]}...")
    print("=" * 60)

    submit_url = "https://stablehorde.net/api/v2/generate/async"
    payload = {
        "prompt": prompt,
        "params": {
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "sampler_name": "k_euler_a",
            "n": 1,
        },
        "models": ["Stable Diffusion XL"],
        "nsfw": False,
        "trusted_workers": False,
        "slow_workers": True,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "apikey": api_key,
        "Client-Agent": "Logovobot:1.0:t.me/logovobot",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(submit_url, data=body, headers=headers)

    try:
        print("⏳ Отправка задачи в AI Horde...")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        job_id = data.get("id")
        print(f"✅ Задача принята. Job ID: {job_id}")
    except urllib.error.HTTPError as e:
        print(f"❌ Ошибка отправки HTTP {e.code}: {e.read().decode()[:300]}")
        return False
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

    # Polling ─── ждём результат
    check_url = f"https://stablehorde.net/api/v2/generate/check/{job_id}"
    status_url = f"https://stablehorde.net/api/v2/generate/status/{job_id}"
    start_t = time.time()
    timeout = 180  # AI Horde может ждать долго (до 3 мин)

    print("⏳ Ожидаем очередь AI Horde (может занять 1-3 минуты)...")
    while time.time() - start_t < timeout:
        time.sleep(10)
        try:
            check_req = urllib.request.Request(check_url, headers={"apikey": api_key, "Client-Agent": "Logovobot:1.0:t.me/logovobot"})
            with urllib.request.urlopen(check_req, timeout=10) as r:
                check_data = json.loads(r.read().decode())
            is_done = check_data.get("done", False)
            wait = check_data.get("wait_time", "?")
            queue = check_data.get("queue_position", "?")
            print(f"   Статус: done={is_done}, wait_time={wait}s, queue={queue} | прошло {int(time.time()-start_t)}s")
            if is_done:
                break
        except Exception as e:
            print(f"   Ошибка проверки: {e}")
    else:
        print(f"❌ Таймаут {timeout}s: AI Horde не ответил")
        return False

    try:
        status_req = urllib.request.Request(status_url, headers={"apikey": api_key, "Client-Agent": "Logovobot:1.0:t.me/logovobot"})
        with urllib.request.urlopen(status_req, timeout=10) as r:
            status_data = json.loads(r.read().decode())
        generations = status_data.get("generations", [])
        if not generations:
            print(f"❌ Нет generations в ответе: {status_data}")
            return False
        img_url = generations[0].get("img")
        print(f"🌐 Картинка доступна по URL: {img_url}")

        img_req = urllib.request.Request(img_url, headers={"User-Agent": "Logovobot/1.0"})
        with urllib.request.urlopen(img_req, timeout=30) as img_resp:
            img_bytes = img_resp.read()

        elapsed = time.time() - start_t
        filename = "test_result_aihorde.webp"
        with open(filename, "wb") as f:
            f.write(img_bytes)
        print(f"✅ Картинка сохранена за {elapsed:.1f} сек, размер: {len(img_bytes)/1024:.1f} KB")
        print(f"📁 Путь: {os.path.abspath(filename)}")
        return True
    except Exception as e:
        print(f"❌ Ошибка получения результата: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ТЕСТ БЕСПЛАТНЫХ ПРОВАЙДЕРОВ ГЕНЕРАЦИИ КАРТИНОК (Logovobot)")
    print("=" * 60)

    PROVIDERS = [
        ("1", "Pollinations.AI (без ключа, бесплатно)"),
        ("2", "AI Horde / Stable Horde (без ключа, бесплатно)"),
        ("all", "Проверить оба"),
    ]

    print("\nДоступные провайдеры:")
    for code, desc in PROVIDERS:
        print(f"  [{code}] {desc}")

    choice = input("\nВыберите провайдер (по умолчанию 1): ").strip().lower() or "1"

    prompt = (
        "Epic European soccer matchday poster for Besiktas JK, majestic black eagle, "
        "black and white club colors, roaring stadium floodlights, green soccer pitch, "
        "dynamic sports poster, ultra-detailed, 4k"
    )

    if choice in ("1", "all"):
        test_pollinations(prompt)

    if choice in ("2", "all"):
        horde_key = input("\nВведите AI Horde ключ (или Enter для анонимного): ").strip()
        if not horde_key:
            horde_key = "0000000000"
        test_aihorde(prompt, api_key=horde_key)

    print("\n" + "=" * 60)
    print("Тест завершен!")
    print("=" * 60)


if __name__ == "__main__":
    main()
