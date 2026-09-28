"""Быстрый тест Novita.ai API для генерации изображений."""
import urllib.request
import urllib.error
import json
import time
import os

NOVITA_KEY = "sk_78xYhn5z4Ki_rVhUvhuaxYblzj4pVEQGdcCx7XUZSJA"
BASE = "https://api.novita.ai"

def call(method, path, payload=None):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {NOVITA_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), r.status

# ─── 1. Проверяем баланс ─────────────────────────────────────────────────────
print("=" * 60)
print("1. Проверка баланса аккаунта")
print("=" * 60)
try:
    data, status = call("GET", "/v3/user")
    print(f"✅ HTTP {status}")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:500])
except urllib.error.HTTPError as e:
    print(f"❌ HTTP {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"❌ {e}")

# ─── 2. Генерация через OpenAI-совместимый эндпоинт ─────────────────────────
print("\n" + "=" * 60)
print("2. Тест генерации (OpenAI-compatible /v3/openai/images/generations)")
print("=" * 60)

prompt = (
    "Epic soccer matchday poster, Besiktas JK, majestic black eagle, "
    "black and white club colors, stadium with floodlights, "
    "cinematic sports art, ultra-detailed, 4k"
)

payload = {
    "model": "flux-schnell",
    "prompt": prompt,
    "n": 1,
    "width": 1024,
    "height": 1024,
    "response_format": "b64_json",
}

start_t = time.time()
try:
    data, status = call("POST", "/v3/openai/images/generations", payload)
    elapsed = time.time() - start_t
    print(f"✅ HTTP {status}, время: {elapsed:.2f} сек")
    b64 = data.get("data", [{}])[0].get("b64_json", "")
    if b64:
        import base64
        img_bytes = base64.b64decode(b64)
        fname = "test_novita_result.png"
        with open(fname, "wb") as f:
            f.write(img_bytes)
        print(f"📁 Сохранено: {os.path.abspath(fname)} ({len(img_bytes)//1024} KB)")
    else:
        print(f"Ответ: {json.dumps(data, indent=2)[:500]}")
except urllib.error.HTTPError as e:
    elapsed = time.time() - start_t
    body = e.read().decode()[:500]
    print(f"❌ HTTP {e.code} за {elapsed:.2f} сек: {body}")
    # Если не поддерживается, попробуем другой эндпоинт
    print("\n→ Пробуем /v3/async/txt2img ...")
    payload2 = {
        "extra": {"response_image_type": "png"},
        "request": {
            "model_name": "flux_1_schnell_fp8_rflow_v10_q5_p.safetensors",
            "prompt": prompt,
            "negative_prompt": "",
            "width": 1024,
            "height": 1024,
            "sampler_name": "Euler a",
            "guidance_scale": 1,
            "steps": 4,
            "image_num": 1,
        }
    }
    try:
        start_t = time.time()
        data2, status2 = call("POST", "/v3/async/txt2img", payload2)
        elapsed2 = time.time() - start_t
        task_id = data2.get("task_id")
        print(f"✅ Задача: task_id={task_id}, HTTP {status2}")

        # polling
        for i in range(24):
            time.sleep(5)
            poll, _ = call("GET", f"/v3/async/task-result?task_id={task_id}")
            state = poll.get("task", {}).get("status", "?")
            print(f"   [{i*5}s] status={state}")
            if state == "TASK_STATUS_SUCCEED":
                imgs = poll.get("images", [])
                if imgs:
                    img_url = imgs[0].get("image_url")
                    print(f"🌐 URL: {img_url}")
                    req_img = urllib.request.Request(img_url, headers={"User-Agent": "Logovobot/1.0"})
                    with urllib.request.urlopen(req_img, timeout=30) as resp:
                        img_bytes = resp.read()
                    fname = "test_novita_result.png"
                    with open(fname, "wb") as f:
                        f.write(img_bytes)
                    print(f"📁 Сохранено: {os.path.abspath(fname)} ({len(img_bytes)//1024} KB)")
                break
            elif "FAIL" in str(state) or "ERROR" in str(state):
                print(f"❌ Ошибка задачи: {poll}")
                break
    except urllib.error.HTTPError as e2:
        print(f"❌ /v3/async/txt2img HTTP {e2.code}: {e2.read().decode()[:300]}")
    except Exception as e2:
        print(f"❌ {e2}")
except Exception as e:
    elapsed = time.time() - start_t
    print(f"❌ {e} ({elapsed:.2f} сек)")
