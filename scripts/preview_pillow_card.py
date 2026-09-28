"""Генерация превью Pillow club card для просмотра."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database
from services import club_smm_service

# Тестируем карточку Бешикташа (самый богатый на данные клуб)
team = "Бешикташ"
print(f"Генерируем Pillow club card для: {team}")

buf = club_smm_service.generate_club_smm_media(team)
if buf:
    fname = "preview_pillow_card.png"
    with open(fname, "wb") as f:
        f.write(buf.read())
    size = os.path.getsize(fname)
    print(f"✅ Сохранено: {os.path.abspath(fname)} ({size//1024} KB)")
else:
    print("❌ Карточка не сгенерирована — нет данных в БД")
    print("   Попробуем другой клуб из БД...")
    import database
    with database.transaction() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM teams LIMIT 10").fetchall()
        print(f"   Клубы: {[r['name'] for r in rows]}")
