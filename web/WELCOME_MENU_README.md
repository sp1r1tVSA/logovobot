# 🐺 Welcome Digest Menu — Руководство по Приветственному Меню

Приветственное меню (**Welcome Screen / Digest**) встречает участников лиги при каждом открытии Telegram Mini-App. Оно погружает менеджеров в актуальный контекст: сообщает о дедлайнах, трансферных окнах, ключевых матчах и дисциплинарных правилах.

Файл интерактивного предпросмотра: [`web/welcome_preview.html`](file:///c:/Users/Ислам/Desktop/Projects/log/logovobot/web/welcome_preview.html)

---

## 📐 Анатомия Интерфейса

Экран построен по модульной архитектуре из 6 ключевых блоков:

```
┌──────────────────────────────────────────────────────────────┐
│ 1. ХЕДЕР УЧАСТНИКА (Приветствие, клуб, тур, дивизион)        │
├──────────────────────────────────────────────────────────────┤
│ 2. СРОЧНАЯ ПЛАШКА (LIVE Ticker — дедлайн или молния)         │
├──────────────────────────────────────────────────────────────┤
│ 3. ЛЕНТА ПЛАШЕК НОВОСТЕЙ (Анонсы, трансферы, регламент, MVP) │
├──────────────────────────────────────────────────────────────┤
│ 4. БИТВА ДНЯ (Центральный матч тура с топ-котировками)       │
├──────────────────────────────────────────────────────────────┤
│ 5. СЕТКА СТАТУСОВ (Трансферный радар + баланс монет)         │
├──────────────────────────────────────────────────────────────┤
│ 6. CTA КНОПКА (Перейти к линии + быстрые ссылки)             │
└──────────────────────────────────────────────────────────────┘
```

---

## 🛠 Конструктор: Как добавлять новые плашки

Все плашки новостей находятся внутри контейнера `<div class="feed-section" id="news-feed-container">`.

### 1. Базовый шаблон плашки
Чтобы добавить новость, просто скопируйте этот блок и вставьте в `feed-section`:

```html
<div class="feed-card" onclick="showNewsDetail('Заголовок при открытии', 'Подробный текст новости...')">
  <div class="feed-icon">🎯</div>
  <div class="feed-content">
    <div class="feed-meta tag-gold">
      <span>КАТЕГОРИЯ</span> • <span class="feed-time">Время</span>
    </div>
    <div class="feed-title">Краткий заголовок плашки в одну строку</div>
  </div>
  <div class="feed-arrow">➔</div>
</div>
```

---

### 🎨 Готовые шаблоны плашек по категориям:

#### 🏆 Кубок / Плей-офф (Синий тег)
```html
<div class="feed-card" onclick="showNewsDetail('Сетка Кубка Лиги', 'Стартовала стадия 1/4 финала Кубка! Матчи серии длятся до 2 побед.')">
  <div class="feed-icon">🏆</div>
  <div class="feed-content">
    <div class="feed-meta tag-blue">
      <span>Кубок Лиги</span> • <span class="feed-time">Сегодня</span>
    </div>
    <div class="feed-title">Стартовала стадия 1/4 финала Кубка Лиги!</div>
  </div>
  <div class="feed-arrow">➔</div>
</div>
```

#### ⚠️ Предупреждения / Штрафы / Варны (Красный тег)
```html
<div class="feed-card" onclick="showNewsDetail('Дисциплинарное предупреждение', 'Клубу Манчестер Сити выписан варн за неявку на матч вовремя.')">
  <div class="feed-icon">🚨</div>
  <div class="feed-content">
    <div class="feed-meta tag-red">
      <span>Дисциплина</span> • <span class="feed-time">14:00</span>
    </div>
    <div class="feed-title">Обновлен список дисциплинарных штрафов участников</div>
  </div>
  <div class="feed-arrow">➔</div>
</div>
```

#### 🪙 Магазин и Награды (Золотой тег)
```html
<div class="feed-card" onclick="showNewsDetail('Новинки в магазине', 'В магазине наград обновлен ассортимент: доступны рулетка фортуны и купоны на скидку!')">
  <div class="feed-icon">🛒</div>
  <div class="feed-content">
    <div class="feed-meta tag-gold">
      <span>Магазин</span> • <span class="feed-time">10:30</span>
    </div>
    <div class="feed-title">Новинки в магазине Mini-app: рулетка и обмен без слотов</div>
  </div>
  <div class="feed-arrow">➔</div>
</div>
```

#### 📊 Статистика и Гонка Бомбардиров (Фиолетовый тег)
```html
<div class="feed-card" onclick="showNewsDetail('Гонка Бомбардиров', 'Эрлинг Холанд догнал Килиана Мбаппе: у обоих по 9 голов в сезоне.')">
  <div class="feed-icon">⚽</div>
  <div class="feed-content">
    <div class="feed-meta tag-purple">
      <span>Статистика</span> • <span class="feed-time">Вчера</span>
    </div>
    <div class="feed-title">Холанд оформил дубль и догнал лидера гонки бомбардиров</div>
  </div>
  <div class="feed-arrow">➔</div>
</div>
```

---

### 🎨 Доступные классы цветов для тегов (`feed-meta`):

| Класс | Цвет | Рекомендуемое назначение |
| :--- | :---: | :--- |
| `tag-gold` | 🟡 Золотой | Анонсы туров, важные новости, магазин |
| `tag-green` | 🟢 Зеленый | Трансферы, спешл-карты, победы, финансы |
| `tag-cyan` | 🔷 Бирюзовый | Регламент, правила, судейство |
| `tag-purple` | 🟣 Фиолетовый | MVP тура, карточки игроков, рекорды |
| `tag-blue` | 🔵 Синий | Кубки, стадии плей-офф, турниры |
| `tag-red` | 🔴 Красный | Штрафы, варны, тех. поражения, дедлайны |

---

## ⚡ Как изменить срочную строку (Breaking Ticker)

Срочная плашка находится прямо под хедером:

```html
<div class="ticker-bar">
  <span class="ticker-pulse">LIVE</span>
  <span class="ticker-text" id="ticker-live-text">🔥 Дедлайн 5-го тура наступит сегодня в 21:00 МСК!</span>
</div>
```
* Чтобы изменить текст, просто поменяйте строку внутри `#ticker-live-text`.
* Если срочных событий нет, плашке можно поставить `style="display: none;"`.

---

## 📊 Как добавить новую информационную плитку в Сетку Статуса (`status-grid`)

В нижней части экрана расположена сетка `<div class="status-grid">`. Сейчас там находятся две плитки: **«Трансферы»** и **«Твой статус»**.

Если вы хотите добавить третью или четвертую плитку (например, **«Позиция в лиге»** или **«Форма команды»**):

```html
<div class="status-tile">
  <div class="tile-header">🏆 Место в таблице</div>
  <div class="tile-val" style="color: var(--accent-cyan);">2 / 16</div>
  <div class="tile-desc">10 очков · В зоне плей-офф</div>
</div>
```

---

## 🚀 Как подключить меню в продакшн Mini-App (`web/index.html` + `web/js/app.js`)

1. **Разметка в `index.html`**:
   Разместите блок `<div id="welcome-digest-overlay">...</div>` поверх основного интерфейса с фиксированным позиционированием `position: fixed; inset: 0; z-index: 500;`.

2. **Логика показа в `app.js`**:
   В функции `loadInitialData()` (после успешной авторизации через Telegram `bootstrap`):
   ```javascript
   // Показываем приветственное меню при входе
   const welcomeModal = document.getElementById('welcome-digest-overlay');
   if (welcomeModal) {
     welcomeModal.style.display = 'flex';
   }
   ```

3. **Закрытие по кнопке «⚽ Перейти к Линии»**:
   ```javascript
   document.getElementById('btn-enter-app')?.addEventListener('click', () => {
     const overlay = document.getElementById('welcome-digest-overlay');
     overlay.classList.add('closing');
     setTimeout(() => {
       overlay.style.display = 'none';
     }, 350);
   });
   ```

4. **Загрузка динамических новостей из бэкенда**:
   Если захотите отдавать новости через API:
   * Эндпоинт `GET /api/news` возвращает массив объектов `{ id, title, full_text, category, tag_color, icon, time }`.
   * JS в цикле отрисовывает карточки через `container.innerHTML = news.map(item => ...)`.
