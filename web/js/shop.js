/**
 * web/js/shop.js
 * Вкладка «🔁 Трансферы» магазина: докупка слотов трансферного окна за монеты.
 * Цену и потолок докупок задаёт ответственный в настройках окна; здесь только
 * показ и покупка — все проверки делает сервер (transfers/slots.py).
 */

import { api } from './api.js';
import { store } from './store.js';
import { tgBridge } from './tg.js';
import { UIRenderer, escapeHtml } from './ui.js';

const SLOT_KINDS = [
  { type: 'buy', icon: '📥', title: 'Слот покупки', hint: 'ещё одна покупка игрока в окне' },
  { type: 'sell', icon: '📤', title: 'Слот продажи', hint: 'ещё одна продажа игрока в окне' },
];

class ShopTransfers {
  constructor() {
    this.root = null;
    this.data = null;
    this.busy = false;
  }

  /** Показать блок слотов в контейнере; для категорий без слотов — скрыть. */
  async show(visible) {
    this.root = document.getElementById('shop-transfers');
    if (!this.root) return;
    this.root.style.display = visible ? '' : 'none';
    if (!visible) return;
    this.root.innerHTML = '<div class="shop-slot-note">Загрузка…</div>';
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
    const cards = SLOT_KINDS.map(k => `
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
    const kind = SLOT_KINDS.find(k => k.type === slotType);
    if (!kind || !confirm(`${kind.title} за ${this.data.price} 🪙?`)) return;
    this.busy = true;
    try {
      const res = await api.buyTransferSlot(slotType);
      this.data = res.purchase.state;
      store.setUser({ ...store.state.user, balance: this.data.balance });
      tgBridge.hapticImpact('medium');
      this.render();
    } catch (e) {
      alert(e.message || 'Не удалось купить слот');
      await this.show(true);
    } finally {
      this.busy = false;
    }
  }
}

export const shopTransfers = new ShopTransfers();
