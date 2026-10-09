/**
 * web/js/shop.js
 *
 * Магазин наград Логова Фифарей:
 * - Каталог из 7 эксклюзивных наград (Тренировки, Трансферный кредит,
 *   Слот обмена, Выгодная урна +25%, Купон на доплату, Секретный игрок)
 * - Интерактивное Колесо Фортуны (Canvas, Web Audio тикер, 8 секторов)
 * - Инвентарь тренера с зарядами предметов
 * - Заявки на секретного игрока и админ-аппрув
 * - Совместимость со слотами ТО (shopTransfers) и пособием (claimBailout)
 */

import { api } from './api.js';
import { store } from './store.js';
import { tgBridge } from './tg.js';
import { UIRenderer, escapeHtml } from './ui.js';

// 8 секторов Колеса Фортуны (соответствуют серверу services/shop_service.py)
const ROULETTE_SECTORS = [
  { id: 'money_1', name: '+10 млн', icon: '💰', color: '#1f242d', textColor: '#ffffff' },
  { id: 'urna', name: 'Выгодная урна', icon: '🗑', color: '#2a2f3a', textColor: '#ffffff' },
  { id: 'train', name: '+5 тренировок', icon: '🏋️', color: '#1d3557', textColor: '#64b5f6' },
  { id: 'coupon', name: 'Купон на доплату', icon: '📄', color: '#163b4d', textColor: '#4dd0e1' },
  { id: 'money_2', name: '+10 млн', icon: '💰', color: '#1f242d', textColor: '#ffffff' },
  { id: 'exchange', name: 'Слот обмена', icon: '🤝', color: '#381a4d', textColor: '#ce93d8' },
  { id: 'credit', name: 'Кредит 20M', icon: '🏦', color: '#4a1525', textColor: '#ff80ab' },
  { id: 'secret', name: 'Секретный игрок', icon: '🕵️', color: '#805b00', textColor: '#ffe082', isLegendary: true },
];

const NUM_SECTORS = ROULETTE_SECTORS.length;
const ARC = (2 * Math.PI) / NUM_SECTORS;
const SPIN_PRICE = 25000;

class ShopManager {
  constructor() {
    this.catalog = null;
    this.inventory = [];
    this.currentCat = 'all';
    this.isSpinning = false;
    this.currentRotation = 0;
    this.canvas = null;
    this.ctx = null;
    this.audioCtx = null;
    this.initialized = false;
  }

  init() {
    if (this.initialized) return;
    this.initialized = true;

    // Инициализация Canvas колеса
    this.canvas = document.getElementById('wheel-canvas');
    if (this.canvas) {
      this.ctx = this.canvas.getContext('2d');
      this.drawWheel();
    }

    // Слушатели событий Колеса
    document.getElementById('wheel-spin-btn')?.addEventListener('click', () => this.spinWheel());
    document.getElementById('center-hub')?.addEventListener('click', () => this.spinWheel());
    document.getElementById('wheel-modal-claim-btn')?.addEventListener('click', () => {
      document.getElementById('wheel-modal')?.classList.remove('active');
    });

    // Слушатель категорий
    document.getElementById('shop-category-pills')?.addEventListener('click', (e) => {
      const btn = e.target.closest('.category-pill');
      if (!btn) return;
      document.querySelectorAll('#shop-category-pills .category-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      this.filterCategory(btn.dataset.shopCat || 'all');
    });
  }

  async load() {
    this.init();
    try {
      const res = await api.getShopCatalog();
      if (res.status === 'ok') {
        this.catalog = res;
        this.inventory = res.inventory || [];
        this.render();
      }
    } catch (e) {
      console.warn('Could not load shop catalog:', e);
    }
  }

  filterCategory(cat) {
    this.currentCat = cat;
    this.render();
  }

  render() {
    const user = store.state.user;
    const balance = user?.balance ?? this.catalog?.balance ?? 0;
    const balanceEl = document.getElementById('shop-balance-val');
    if (balanceEl) {
      balanceEl.textContent = UIRenderer.formatNumber(balance);
    }

    const grid = document.getElementById('shop-items-grid');
    const wheelSec = document.getElementById('shop-wheel-section');
    const invSec = document.getElementById('shop-inventory-section');

    const showWheel = this.currentCat === 'all' || this.currentCat === 'roulette';
    const showInv = this.currentCat === 'all' || this.currentCat === 'inventory';
    const showItems = this.currentCat !== 'roulette' && this.currentCat !== 'inventory';

    if (wheelSec) wheelSec.style.display = showWheel ? 'flex' : 'none';
    if (invSec) invSec.style.display = showInv ? 'block' : 'none';
    if (grid) grid.style.display = showItems ? 'grid' : 'none';

    if (showItems && grid && this.catalog?.items) {
      this.renderItems(grid, balance);
    }

    if (showInv) {
      this.renderInventory();
    }

    // Обновляем кнопку спина колеса
    const spinBtn = document.getElementById('wheel-spin-btn');
    const spinsThisSeason = this.catalog?.roulette_spins_this_season ?? 0;
    const isSeasonSpinUsed = spinsThisSeason >= 1;
    if (spinBtn) {
      if (isSeasonSpinUsed) {
        spinBtn.disabled = true;
        spinBtn.textContent = 'Лимит сезона (1/1)';
      } else {
        spinBtn.disabled = this.isSpinning || balance < SPIN_PRICE;
        spinBtn.textContent = 'Крутить за 25 000 🪙';
      }
    }
  }

  renderItems(container, balance) {
    let items = this.catalog.items || [];
    if (this.currentCat !== 'all') {
      items = items.filter(it => it.category === this.currentCat);
    }

    // Исключаем саму рулетку из обычной сетки карточек, так как под неё есть отдельный большой блок
    items = items.filter(it => it.id !== 'roulette_spin');

    if (items.length === 0) {
      container.innerHTML = '<div class="inventory-empty" style="grid-column: 1 / -1;">В этой категории пока нет товаров.</div>';
      return;
    }

    const isWindowOpen = Boolean(this.catalog?.is_window_open);
    const bought = this.catalog?.window_transfer_rewards_bought ?? 0;
    const limit = this.catalog?.window_transfer_rewards_limit ?? 2;
    const windowTitle = this.catalog?.window_title || 'Трансферное окно';

    let windowBannerHtml = '';
    if (isWindowOpen && (this.currentCat === 'all' || this.currentCat === 'transfers')) {
      const isLimitFull = bought >= limit;
      windowBannerHtml = `
        <div class="shop-window-limit-banner ${isLimitFull ? 'limit-reached' : ''}" style="grid-column: 1 / -1;">
          <div class="swl-info">
            <span class="swl-icon">⏳</span>
            <div>
              <div class="swl-title">${escapeHtml(windowTitle)}: лимит наград ТО</div>
              <div class="swl-desc">В одно окно доступно максимум <strong>${limit} награды</strong> для трансферов (от 5 500 до 10 000 🪙).</div>
            </div>
          </div>
          <div class="swl-counter">
            <span class="swl-count ${isLimitFull ? 'full' : ''}">${bought} / ${limit}</span>
            <span class="swl-sub">${isLimitFull ? 'Лимит исчерпан' : 'куплено'}</span>
          </div>
        </div>
      `;
    }

    const cardsHtml = items.map(it => {
      const canBuy = it.available && balance >= it.price;
      const isTop = it.id === 'secret_player' || it.id === 'urna_boost';
      let windowBadge = '';
      if (it.requires_window) {
        if (!it.available && it.reason && it.reason.includes('Лимит')) {
          windowBadge = '<span class="shop-card-badge tag-limit-reached">Лимит исчерпан</span>';
        } else {
          windowBadge = '<span class="shop-card-badge tag-window">Трансферное окно</span>';
        }
      } else if (it.limit_scope === 'season' && !it.available && it.reason && it.reason.includes('Лимит')) {
        windowBadge = '<span class="shop-card-badge tag-limit-reached">Лимит сезона</span>';
      }
      return `
        <div class="shop-card ${isTop ? 'featured' : ''}" data-item-id="${escapeHtml(it.id)}">
          <div class="shop-card-header">
            <div class="shop-card-icon">${it.icon}</div>
            <div class="shop-card-headings">
              <div class="shop-card-title">${escapeHtml(it.name)}</div>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <span class="shop-card-badge">${escapeHtml(it.badge)}</span>
                ${windowBadge}
              </div>
            </div>
          </div>
          <div class="shop-card-desc">${escapeHtml(it.description)}</div>
          ${!it.available && it.reason ? `<div class="shop-card-reason">⚠️ ${escapeHtml(it.reason)}</div>` : ''}
          <div class="shop-card-footer">
            <div class="shop-card-price">
              <span>${UIRenderer.formatNumber(it.price)}</span> 🪙
            </div>
            <button class="shop-card-buy-btn" data-buy="${escapeHtml(it.id)}" ${canBuy ? '' : 'disabled'}>
              Купить
            </button>
          </div>
        </div>
      `;
    }).join('');

    container.innerHTML = windowBannerHtml + cardsHtml;

    container.querySelectorAll('.shop-card-buy-btn').forEach(btn => {
      btn.addEventListener('click', () => this.buyItem(btn.dataset.buy));
    });
  }

  renderInventory() {
    const list = document.getElementById('shop-inventory-list');
    const badge = document.getElementById('inventory-count-badge');
    if (!list) return;

    const activeItems = (this.inventory || []).filter(i => i.status === 'active' && i.charges_left > 0);
    if (badge) badge.textContent = `${activeItems.length} активных`;

    if (activeItems.length === 0) {
      list.innerHTML = '<div class="inventory-empty">У вас пока нет активных наград. Приобретайте усиления в магазине или крутите рулетку!</div>';
      return;
    }

    const ICON_MAP = {
      train_5: '🏋️',
      credit_transfer: '🏦',
      slot_swap: '🤝',
      urna_boost: '🗑',
      surcharge_coupon: '📄',
      secret_player: '🕵️',
      budget_10m: '💰',
    };

    const NAME_MAP = {
      train_5: 'Тренировка (+5)',
      credit_transfer: 'Трансферный кредит (до -20M)',
      slot_swap: 'Слот обмена игроками',
      urna_boost: 'Выгодная урна (+1 игрок в урну)',
      surcharge_coupon: 'Купон на доплату (50% на спешл)',
      secret_player: 'Секретный игрок',
      budget_10m: 'Трансферный бюджет (+10M)',
    };

    list.innerHTML = activeItems.map(item => {
      const icon = ICON_MAP[item.item_id] || '🎁';
      const name = NAME_MAP[item.item_id] || item.item_id;
      return `
        <div class="inventory-item-card">
          <div class="inventory-item-left">
            <span class="inventory-item-icon">${icon}</span>
            <div>
              <div class="inventory-item-name">${escapeHtml(name)}</div>
              <div class="inventory-item-detail">Клуб: ${escapeHtml(item.club_name || '—')}</div>
            </div>
          </div>
          <div class="inventory-item-charges">${item.charges_left} ${item.charges_left === 1 ? 'заряд' : 'заряда'}</div>
        </div>
      `;
    }).join('');
  }

  async buyItem(itemId) {
    const target = (this.catalog?.items || []).find(it => it.id === itemId);
    if (!target) return;

    let notes = null;
    if (itemId === 'secret_player') {
      notes = prompt('Укажите пожелания по позиции или характеристикам игрока для админа (опционально):');
      if (notes === null) return; // отмена
    } else {
      if (!confirm(`Приобрести «${target.name}» за ${UIRenderer.formatNumber(target.price)} 🪙?`)) {
        return;
      }
    }

    try {
      const res = await api.buyShopItem(itemId, notes);
      if (res.status === 'ok') {
        tgBridge.hapticImpact('heavy');
        alert(res.message || 'Успешно приобретено!');
        if (res.balance !== undefined) {
          store.setUser({ ...store.state.user, balance: res.balance });
        }
        await this.load();
      } else {
        alert(res.message || 'Ошибка покупки');
      }
    } catch (e) {
      alert(e.message || 'Не удалось совершить покупку');
    }
  }

  // --- ЛОГИКА И ОТРИСОВКА КОЛЕСА ФОРТУНЫ ---

  initAudio() {
    if (!this.audioCtx) {
      try {
        this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      } catch (e) {}
    }
  }

  playClick() {
    if (!this.audioCtx) this.initAudio();
    try {
      if (this.audioCtx.state === 'suspended') this.audioCtx.resume();
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(1400, this.audioCtx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(300, this.audioCtx.currentTime + 0.025);
      gain.gain.setValueAtTime(0.09, this.audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.audioCtx.currentTime + 0.025);
      osc.connect(gain);
      gain.connect(this.audioCtx.destination);
      osc.start();
      osc.stop(this.audioCtx.currentTime + 0.025);

      const pointer = document.getElementById('pointer-arrow');
      if (pointer) {
        pointer.style.transform = 'rotate(-12deg)';
        setTimeout(() => { pointer.style.transform = 'rotate(0deg)'; }, 45);
      }
    } catch (e) {}
  }

  drawWheel() {
    if (!this.canvas || !this.ctx) return;
    const size = this.canvas.width;
    const center = size / 2;
    const radius = center - 14;

    this.ctx.clearRect(0, 0, size, size);

    for (let i = 0; i < NUM_SECTORS; i++) {
      const sector = ROULETTE_SECTORS[i];
      const angle = i * ARC;

      this.ctx.save();
      this.ctx.beginPath();
      this.ctx.moveTo(center, center);
      this.ctx.arc(center, center, radius, angle, angle + ARC);
      this.ctx.closePath();

      this.ctx.fillStyle = sector.color;
      this.ctx.fill();

      this.ctx.strokeStyle = sector.isLegendary ? '#ffb703' : 'rgba(255, 255, 255, 0.12)';
      this.ctx.lineWidth = sector.isLegendary ? 4 : 2;
      this.ctx.stroke();

      if (sector.isLegendary) {
        this.ctx.save();
        this.ctx.clip();
        const grad = this.ctx.createRadialGradient(center, center, 40, center, center, radius);
        grad.addColorStop(0, 'rgba(255, 183, 3, 0.1)');
        grad.addColorStop(1, 'rgba(255, 183, 3, 0.45)');
        this.ctx.fillStyle = grad;
        this.ctx.fill();
        this.ctx.restore();
      }

      this.ctx.save();
      this.ctx.translate(center, center);
      this.ctx.rotate(angle + ARC / 2);

      this.ctx.font = '36px "Outfit", sans-serif';
      this.ctx.textAlign = 'center';
      this.ctx.textBaseline = 'middle';
      this.ctx.fillText(sector.icon, radius * 0.82, 0);

      let fontSize = 26;
      this.ctx.font = `800 ${fontSize}px "Outfit", sans-serif`;
      const maxTextWidth = radius * 0.52;
      while (this.ctx.measureText(sector.name).width > maxTextWidth && fontSize > 18) {
        fontSize -= 1;
        this.ctx.font = `800 ${fontSize}px "Outfit", sans-serif`;
      }

      this.ctx.fillStyle = sector.textColor;
      this.ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
      this.ctx.shadowBlur = 8;
      this.ctx.shadowOffsetX = 0;
      this.ctx.shadowOffsetY = 2;
      this.ctx.fillText(sector.name, radius * 0.47, 0);

      this.ctx.restore();
      this.ctx.restore();
    }

    // Внешний обод
    this.ctx.beginPath();
    this.ctx.arc(center, center, radius, 0, 2 * Math.PI);
    this.ctx.lineWidth = 10;
    this.ctx.strokeStyle = '#1d2027';
    this.ctx.stroke();

    // Пины
    for (let i = 0; i < NUM_SECTORS * 2; i++) {
      const pinAngle = i * (Math.PI / NUM_SECTORS);
      const px = center + (radius - 2) * Math.cos(pinAngle);
      const py = center + (radius - 2) * Math.sin(pinAngle);

      this.ctx.beginPath();
      this.ctx.arc(px, py, 4, 0, 2 * Math.PI);
      this.ctx.fillStyle = '#ffcc00';
      this.ctx.fill();
      this.ctx.shadowColor = '#ffcc00';
      this.ctx.shadowBlur = 6;
    }
  }

  async spinWheel() {
    if (this.isSpinning) return;
    const balance = store.state.user?.balance ?? 0;
    if (balance < SPIN_PRICE) {
      alert(`Для прокрута рулетки нужно ${UIRenderer.formatNumber(SPIN_PRICE)} 🪙`);
      return;
    }

    this.isSpinning = true;
    const spinBtn = document.getElementById('wheel-spin-btn');
    if (spinBtn) spinBtn.disabled = true;

    try {
      const res = await api.spinRoulette();
      if (res.status !== 'ok') {
        alert(res.message || 'Ошибка вращения');
        this.isSpinning = false;
        if (spinBtn) spinBtn.disabled = false;
        return;
      }

      // Обновляем локальный баланс
      store.setUser({ ...store.state.user, balance: res.balance });
      const balanceEl = document.getElementById('shop-balance-val');
      if (balanceEl) balanceEl.textContent = UIRenderer.formatNumber(res.balance);

      const winIndex = res.winning_index ?? 0;
      const winningSector = ROULETTE_SECTORS[winIndex];

      const degPerSector = 360 / NUM_SECTORS;
      const sectorCenterDeg = (winIndex * degPerSector) + (degPerSector / 2);
      const extraSpins = 360 * 12; // 12 полных оборотов
      const jitter = (Math.random() * 16) - 8;

      const targetSectorAngle = 270 - sectorCenterDeg;
      const currentModulo = this.currentRotation % 360;
      let diff = targetSectorAngle - currentModulo;
      if (diff < 0) diff += 360;

      const finalRotation = this.currentRotation + extraSpins + diff + jitter;
      const startRotation = this.currentRotation;
      const totalDelta = finalRotation - startRotation;
      const duration = 9000; // 9 секунд захватывающей анимации
      const startTime = performance.now();

      let lastClickTick = 0;

      const animate = (currentTime) => {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);

        // Quintic Easing Out: супер-динамичный старт и плавнейшая остановка
        const easeProgress = 1 - Math.pow(1 - progress, 5);

        const currentAngle = startRotation + totalDelta * easeProgress;
        this.currentRotation = currentAngle;
        if (this.canvas) {
          this.canvas.style.transform = `rotate(${currentAngle}deg)`;
        }

        // Звук трещотки
        const sectorProgress = Math.floor(currentAngle / degPerSector);
        if (sectorProgress !== lastClickTick) {
          this.playClick();
          tgBridge.hapticImpact('light');
          lastClickTick = sectorProgress;
        }

        if (progress < 1) {
          requestAnimationFrame(animate);
        } else {
          this.isSpinning = false;
          tgBridge.hapticImpact('heavy');
          this.showWinnerModal(winningSector, res.reward_label);
          this.load(); // обновляем инвентарь
        }
      };

      requestAnimationFrame(animate);

    } catch (e) {
      alert(e.message || 'Ошибка связи с сервером');
      this.isSpinning = false;
      if (spinBtn) spinBtn.disabled = false;
    }
  }

  showWinnerModal(sector, label) {
    const modal = document.getElementById('wheel-modal');
    const iconEl = document.getElementById('wheel-modal-icon');
    const titleEl = document.getElementById('wheel-modal-title');
    const descEl = document.getElementById('wheel-modal-desc');

    if (!modal) return;
    if (iconEl) iconEl.textContent = sector.icon;
    if (titleEl) titleEl.textContent = sector.name;
    if (descEl) descEl.textContent = label || sector.name;

    modal.classList.add('active');
  }
}

export const shopManager = new ShopManager();

// Сохраняем существующую структуру для совместимости с app.js и вызовов слотов ТО
class ShopTransfers {
  constructor() {
    this.root = null;
    this.data = null;
    this.busy = false;
  }

  async show(visible) {
    this.root = document.getElementById('shop-transfers');
    if (!this.root) return;
    this.root.style.display = visible ? '' : 'none';
    if (!visible) return;
    this.root.innerHTML = '<div class="shop-slot-note">Загрузка слотов…</div>';
    try {
      const res = await api.getTransferSlots();
      this.data = res.data;
    } catch (e) {
      this.data = null;
      this.root.innerHTML = `<div class="shop-slot-note">${escapeHtml(e.message || 'Не удалось загрузить слоты')}</div>`;
      return;
    }
    this.render();
  }

  render() {
    const d = this.data;
    if (!d) return;
    const head = d.price
      ? `<div class="shop-slot-head"><span>${escapeHtml(d.club || '')}</span>`
        + `<span>Докуплено ${d.bought || 0} из ${d.max_extra || 0}</span></div>`
      : '';
    const note = d.available ? '' : `<div class="shop-slot-note">${escapeHtml(d.reason || '')}</div>`;
    const cards = [
      { type: 'buy', icon: '📥', title: 'Слот покупки', hint: 'ещё одна покупка игрока в окне' },
      { type: 'sell', icon: '📤', title: 'Слот продажи', hint: 'ещё одна продажа игрока в окне' },
    ].map(k => `
      <div class="shop-slot-card">
        <div class="shop-slot-icon">${k.icon}</div>
        <div class="shop-slot-info">
          <div class="shop-slot-title">${k.title}</div>
          <div class="shop-slot-hint">${k.hint}</div>
        </div>
        <button class="shop-slot-buy" data-slot="${k.type}" ${d.available ? '' : 'disabled'}>
          ${d.price ? `${UIRenderer.formatNumber(d.price)} 🪙` : '—'}
        </button>
      </div>`).join('');
    this.root.innerHTML = head + cards + note;
    this.root.querySelectorAll('.shop-slot-buy').forEach(btn => {
      btn.addEventListener('click', () => this.buy(btn.dataset.slot));
    });
  }

  async buy(slotType) {
    if (this.busy || !this.data?.available) return;
    if (!confirm(`Купить слот за ${this.data.price} 🪙?`)) return;
    this.busy = true;
    try {
      const res = await api.buyTransferSlot(slotType);
      this.data = res.purchase.state;
      store.setUser({ ...store.state.user, balance: this.data.balance });
      tgBridge.hapticImpact('medium');
      this.render();
      shopManager.load();
    } catch (e) {
      alert(e.message || 'Не удалось купить слот');
      await this.show(true);
    } finally {
      this.busy = false;
    }
  }
}

export const shopTransfers = new ShopTransfers();

// Пособие при нулевом балансе
let bailoutBusy = false;
async function claimBailout(btn) {
  if (bailoutBusy) return;
  bailoutBusy = true;
  btn.disabled = true;
  try {
    const res = await api.claimBailout();
    store.setUser({ ...store.state.user, balance: res.bailout.balance, bailout: res.bailout });
    tgBridge.hapticImpact('medium');
    shopManager.load();
  } catch (e) {
    const fresh = e.data?.bailout;
    if (fresh) store.setUser({ ...store.state.user, bailout: fresh });
    else btn.disabled = false;
    alert(e.message || 'Не удалось получить пособие');
  } finally {
    bailoutBusy = false;
  }
}

document.addEventListener('click', (ev) => {
  const btn = ev.target.closest?.('#shop-bailout-claim');
  if (btn) claimBailout(btn);
});
