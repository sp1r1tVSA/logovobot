/**
 * web/js/transfers.js
 * Интерфейс трансферного окна («ТО») в Telegram Mini App.
 *
 * 5 вкладок:
 *  1. Статус   — состояние окна, бюджет и слоты клуба, входящие предложения, мои заявки
 *  2. Заявка   — форма подачи сделки / доплаты за спешл / продажи в урну (с фото)
 *  3. Рынок    — урна (выкуп) и каталог игроков с поиском и фильтрами; из каталога — в форму сделки
 *  4. Доска    — лоты «ищу / продаю»; отклик открывает форму сделки, привязанную к лоту
 *  5. История  — лента одобренных трансферов лиги (с просмотром фото)
 */

import { api } from './api.js';
import { tgBridge } from './tg.js';
import { escapeHtml, getTeamLogoUrl } from './ui.js';

class TransfersView {
  constructor() {
    this.root = null;
    this.activeTab = 'status';
    this.requestKind = 'deal';
    this.dealRole = 'buy';
    this.statusData = null;
    this.marketData = null;
    this.marketFilters = { q: '', ovrMin: '', ovrMax: '', club: '', sort: 'ovr' };
    this.prefill = null;
    this.boardData = null;
    this.boardSide = 'sell';
    this.respondLot = null; // { id, role, label } — заявка уйдёт откликом на этот лот
    this.historyData = null;
    this.loading = false;
    this.submitting = false;
    this.selectedPhotoFile = null;
    this.photoPreviewUrl = null;
  }

  init() {
    this.root = document.getElementById('transfers-root');
    if (!this.root) return;
    this.renderShell();
    this.loadActiveTab();
  }

  renderShell() {
    this.root.innerHTML = `
      <div class="transfers-wrap">
        <div class="transfers-header-row">
          <div class="transfers-page-title">🔁 Трансферы</div>
          <button class="transfers-refresh-btn" id="transfers-refresh-btn" type="button" title="Обновить">🔄</button>
        </div>

        <nav class="transfers-tabs" role="tablist">
          <button class="transfers-tab-btn ${this.activeTab === 'status' ? 'active' : ''}" data-tab="status" type="button">📊 Статус</button>
          <button class="transfers-tab-btn ${this.activeTab === 'request' ? 'active' : ''}" data-tab="request" type="button">📝 Заявка</button>
          <button class="transfers-tab-btn ${this.activeTab === 'market' ? 'active' : ''}" data-tab="market" type="button">🛒 Рынок</button>
          <button class="transfers-tab-btn ${this.activeTab === 'board' ? 'active' : ''}" data-tab="board" type="button">📌 Доска</button>
          <button class="transfers-tab-btn ${this.activeTab === 'history' ? 'active' : ''}" data-tab="history" type="button">📜 История</button>
        </nav>

        <div id="transfers-tab-content">
          <div class="transfers-empty">
            <div class="transfers-empty-icon">⏳</div>
            <div>Загрузка данных...</div>
          </div>
        </div>
      </div>

      <!-- Photo Zoom Modal -->
      <div class="modal-overlay" id="transfers-photo-modal">
        <div class="modal-content" style="max-width: 480px; text-align: center;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <span style="font-weight: 800; color: #fff;">📸 Фото заявки</span>
            <button class="btn-modal-close btn-modal-x" id="btn-close-photo-modal" aria-label="Закрыть">✕</button>
          </div>
          <div id="transfers-photo-modal-body" style="min-height: 200px; display: flex; align-items: center; justify-content: center;">
            <div style="color: var(--text-muted);">Загрузка фото...</div>
          </div>
        </div>
      </div>
    `;

    this.bindEvents();
  }

  bindEvents() {
    this.root.querySelectorAll('.transfers-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (this.activeTab === tab) return;
        this.activeTab = tab;
        this.root.querySelectorAll('.transfers-tab-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.tab === tab);
        });
        tgBridge.hapticImpact('light');
        this.loadActiveTab();
      });
    });

    document.getElementById('transfers-refresh-btn')?.addEventListener('click', () => {
      tgBridge.hapticImpact('light');
      this.loadActiveTab(true);
    });

    document.getElementById('btn-close-photo-modal')?.addEventListener('click', () => {
      document.getElementById('transfers-photo-modal')?.classList.remove('active');
    });
  }

  async loadActiveTab(force = false) {
    const container = document.getElementById('transfers-tab-content');
    if (!container) return;

    if (this.activeTab === 'status') {
      await this.loadStatus(container, force);
    } else if (this.activeTab === 'request') {
      this.renderRequestTab(container);
    } else if (this.activeTab === 'market') {
      await this.loadMarket(container, force);
    } else if (this.activeTab === 'board') {
      await this.loadBoard(container, force);
    } else if (this.activeTab === 'history') {
      await this.loadHistory(container, force);
    }
  }

  // ─── 1. Вкладка «Статус» ───────────────────────────────────────────────────

  async loadStatus(container, force = false) {
    if (!this.statusData || force) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">⏳</div>
          <div>Загрузка статуса...</div>
        </div>`;
      try {
        const res = await api.getTransferStatus();
        if (res.status === 'ok') {
          this.statusData = res.data;
        } else {
          container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(res.message || 'Ошибка загрузки')}</div>`;
          return;
        }
      } catch (err) {
        container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(err.message || 'Ошибка сети')}</div>`;
        return;
      }
    }

    const { window: win, club, ledger, requests, rules, sanction } = this.statusData;

    let winHtml = '';
    if (!win) {
      winHtml = `
        <div class="transfers-card">
          <div class="window-banner-head">
            <span class="window-status-badge badge-closed">🔒 Окно закрыто</span>
          </div>
          <div style="font-size: 0.9rem; color: var(--text-secondary); line-height: 1.4;">
            Трансферное окно в данный момент закрыто. Следите за анонсами в канале лиги.
          </div>
        </div>`;
    } else {
      const isDraft = win.status === 'draft';
      const isOpen = win.status === 'open';
      const badgeCls = isOpen ? 'badge-open' : (isDraft ? 'badge-draft' : 'badge-closed');
      const badgeTxt = isOpen ? '🟢 Окно открыто' : (isDraft ? '📝 Черновик' : '🔒 Закрыто');
      const title = win.title ? `«${escapeHtml(win.title)}»` : `Окно #${win.id}`;

      winHtml = `
        <div class="transfers-card">
          <div class="window-banner-head">
            <span style="font-family: 'Outfit', sans-serif; font-size: 1.05rem; font-weight: 800; color: #fff;">${title}</span>
            <span class="window-status-badge ${badgeCls}">${badgeTxt}</span>
          </div>
          ${win.auto_close_at ? `<div class="window-meta-time">⏱ Закрытие: <b>${escapeHtml(win.auto_close_at)} МСК</b></div>` : ''}
          ${rules ? `
            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px;">
              <span class="req-ovr-tag">OVR cap: ${rules.ovr_cap}</span>
              <span class="req-ovr-tag">Ядро состава: ${rules.min_core_players}+</span>
            </div>` : ''}
        </div>`;
    }

    let sanctionHtml = '';
    if (sanction) {
      const who = sanction.scope === 'coach' ? 'Вы лишены трансферного окна' : `Клуб ${escapeHtml(sanction.club || club || '')} лишён трансферного окна`;
      const left = sanction.seasons_left;
      const leftWord = left === 1 ? 'сезон' : (left > 1 && left < 5 ? 'сезона' : 'сезонов');
      sanctionHtml = `
        <div class="transfers-card sanction-banner">
          <div class="sanction-banner-title">⛔ ${who}</div>
          <div class="sanction-banner-text">
            Заявки и докупка слотов недоступны. Осталось: <b>${left} ${leftWord}</b>, считая текущий.
            ${sanction.reason ? `<br>Причина: ${escapeHtml(sanction.reason)}` : ''}
          </div>
        </div>`;
    }

    let clubHtml = '';
    if (club && ledger) {
      clubHtml = `
        <div class="transfers-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <span style="font-family: 'Outfit', sans-serif; font-size: 1.05rem; font-weight: 800; color: #fff;">🛡 ${escapeHtml(club)}</span>
            <span style="font-size: 0.78rem; color: var(--text-muted);">Бюджет клуба</span>
          </div>
          <div class="ledger-grid">
            <div>
              <div class="ledger-stat-label">Бюджет</div>
              <div class="ledger-stat-val">${(ledger.budget_k / 1000).toFixed(1)}M</div>
            </div>
            <div>
              <div class="ledger-stat-label">Потрачено</div>
              <div class="ledger-stat-val">${(ledger.spent_k / 1000).toFixed(1)}M</div>
            </div>
            <div>
              <div class="ledger-stat-label">Получено</div>
              <div class="ledger-stat-val">${(ledger.earned_k / 1000).toFixed(1)}M</div>
            </div>
            <div>
              <div class="ledger-stat-label">Остаток</div>
              <div class="ledger-stat-val gold">${(ledger.remaining_k / 1000).toFixed(1)}M</div>
            </div>
          </div>
          <div class="slots-row">
            <div class="slot-pill">
              <span class="slot-pill-label">Покупки:</span>
              <span class="slot-pill-val">${ledger.buys_used} / ${ledger.buys_limit}</span>
            </div>
            <div class="slot-pill">
              <span class="slot-pill-label">Продажи:</span>
              <span class="slot-pill-val">${ledger.sells_used} / ${ledger.sells_limit}</span>
            </div>
            <div class="slot-pill">
              <span class="slot-pill-label">Урна:</span>
              <span class="slot-pill-val">${ledger.urn_sales} / ${ledger.urn_limit}</span>
            </div>
          </div>
        </div>`;
    } else if (!club) {
      clubHtml = `
        <div class="transfers-card" style="border-left: 3px solid #f5b027;">
          <div style="font-size: 0.88rem; color: var(--text-secondary); line-height: 1.4;">
            ⚠️ Вы не зарегистрированы за клубом лиги. Подача заявок доступна только тренерам клубов.
          </div>
        </div>`;
    }

    // Входящие подтверждения
    const reqList = this.collapseSwaps(requests || []);
    const incomingDeals = reqList.filter(r => r.can_confirm);
    let incomingHtml = '';
    if (incomingDeals.length > 0) {
      incomingHtml = `
        <div class="incoming-deals-box">
          <div class="incoming-deals-title">⚡️ Ждут вашего подтверждения (${incomingDeals.length})</div>
          ${incomingDeals.map(r => this.renderRequestItem(r)).join('')}
        </div>`;
    }

    // Мои заявки
    const ownRequests = reqList.filter(r => !r.can_confirm);
    let ownHtml = `
      <div style="margin-top: 14px;">
        <div style="font-family: 'Outfit', sans-serif; font-size: 0.95rem; font-weight: 800; color: #fff; margin-bottom: 8px;">
          📋 Ваши заявки (${requests ? requests.length : 0})
        </div>
        ${ownRequests.length === 0 ? '<div class="transfers-empty"><div class="transfers-empty-icon">📭</div><div>Заявок пока нет</div></div>' : ownRequests.map(r => this.renderRequestItem(r)).join('')}
      </div>`;

    container.innerHTML = winHtml + sanctionHtml + clubHtml + incomingHtml + ownHtml;
    this.bindActionButtons(container);
  }

  // Обмен приходит двумя заявками; в списке показываем одну карточку (первую половину),
  // а вторую цепляем к ней как `_swap`. Кнопки действуют на пару на сервере.
  collapseSwaps(list) {
    const byId = new Map(list.map(r => [r.id, r]));
    const out = [];
    for (const r of list) {
      const pid = r.swap_partner_id;
      if (!pid || !byId.has(pid)) { out.push(r); continue; }
      if (r.id < pid) out.push({ ...r, _swap: byId.get(pid) });
    }
    return out;
  }

  renderRequestItem(r) {
    const kindMap = {
      deal: '🤝 Сделка',
      surcharge: '⚡ Спешл',
      urn_sale: '🗑 Продажа в урну',
      urn_buy: '🛍 Выкуп из урны',
      free_agent: '🏃 СА',
    };
    const statusMap = {
      pending_counterparty: { label: 'Ждёт стороны', cls: 'badge-draft' },
      pending_manager: { label: 'На рассмотрении', cls: 'badge-draft' },
      approved: { label: 'Одобрено', cls: 'badge-open' },
      rejected: { label: 'Отклонено', cls: 'badge-closed' },
      withdrawn: { label: 'Отозвано', cls: 'badge-closed' },
      cancelled: { label: 'Отменено', cls: 'badge-closed' },
    };
    const st = statusMap[r.status] || { label: r.status, cls: 'badge-closed' };

    return `
      <div class="req-item" data-id="${r.id}">
        <div class="req-item-head">
          <span class="req-item-title">
            ${r.swap_partner_id ? '🔁 Обмен' : (kindMap[r.kind] || r.kind)} #${r.id}${r._swap ? `+#${r._swap.id}` : ''}: <b>${escapeHtml(r.player_name)}</b>
            ${r.ovr ? `<span class="req-ovr-tag">OVR ${r.ovr}</span>` : ''}
          </span>
          <span class="window-status-badge ${st.cls}">${st.label}</span>
        </div>

        <div class="req-route hist-route">
          ${r.from_club ? this.renderRouteClub(r.from_club) : ''}
          ${r.from_club && r.to_club ? '<span class="hist-arrow">→</span>' : ''}
          ${r.to_club ? this.renderRouteClub(r.to_club) : ''}
        </div>
        ${r._swap ? `
          <div class="req-item-title" style="margin-top: 4px;">
            ⇄ <b>${escapeHtml(r._swap.player_name)}</b>
            ${r._swap.ovr ? `<span class="req-ovr-tag">OVR ${r._swap.ovr}</span>` : ''}
            <span style="color: var(--text-muted); font-size: 0.78rem;">${r._swap.price}</span>
          </div>` : ''}

        ${r.warnings && r.warnings.length ? `
          <div style="font-size: 0.74rem; color: var(--accent-gold); margin: 4px 0;">
            ${r.warnings.map(w => `• ${escapeHtml(w.message || w)}`).join('<br>')}
          </div>` : ''}

        ${r.decided_reason ? `
          <div style="font-size: 0.74rem; color: var(--color-danger); margin: 4px 0;">
            Причина: ${escapeHtml(r.decided_reason)}
          </div>` : ''}

        <div class="req-footer">
          <div class="req-price">${r.price}</div>
          <div class="req-actions">
            ${r.has_photo ? `<button class="btn-withdraw btn-view-photo" data-id="${r.id}" type="button">📸 Фото</button>` : ''}
            ${r.can_confirm ? `
              <button class="btn-confirm btn-req-confirm" data-id="${r.id}" type="button">✅ Принять</button>
              <button class="btn-decline btn-req-decline" data-id="${r.id}" type="button">❌ Отклонить</button>` : ''}
            ${r.can_withdraw ? `
              <button class="btn-withdraw btn-req-withdraw" data-id="${r.id}" type="button">✖️ Отозвать</button>` : ''}
          </div>
        </div>
      </div>`;
  }

  bindActionButtons(container) {
    container.querySelectorAll('.btn-req-confirm').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          await api.confirmTransfer(id);
          tgBridge.hapticImpact('medium');
          this.loadStatus(container, true);
        } catch (e) {
          alert(e.message || 'Ошибка');
          btn.disabled = false;
          btn.textContent = '✅ Принять';
        }
      });
    });

    container.querySelectorAll('.btn-req-decline').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          await api.declineTransfer(id);
          tgBridge.hapticImpact('light');
          this.loadStatus(container, true);
        } catch (e) {
          alert(e.message || 'Ошибка');
          btn.disabled = false;
          btn.textContent = '❌ Отклонить';
        }
      });
    });

    container.querySelectorAll('.btn-req-withdraw').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Отозвать заявку?')) return;
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          await api.withdrawTransfer(id);
          tgBridge.hapticImpact('light');
          this.loadStatus(container, true);
        } catch (e) {
          alert(e.message || 'Ошибка');
          btn.disabled = false;
          btn.textContent = '✖️ Отозвать';
        }
      });
    });

    container.querySelectorAll('.btn-view-photo').forEach(btn => {
      btn.addEventListener('click', () => {
        this.openPhotoModal(btn.dataset.id);
      });
    });
  }

  openPhotoModal(transferId) {
    const modal = document.getElementById('transfers-photo-modal');
    const body = document.getElementById('transfers-photo-modal-body');
    if (!modal || !body) return;

    modal.classList.add('active');
    body.innerHTML = `
      <img src="/api/transfers/${transferId}/photo" alt="Фото заявки #${transferId}"
           style="max-width: 100%; max-height: 70vh; border-radius: 12px; object-fit: contain;"
           onload="this.style.opacity='1'" onerror="this.parentNode.innerHTML='<div style=\\'color:var(--color-danger);\\'>Не удалось загрузить фото</div>'">
    `;
  }

  // ─── 2. Вкладка «Заявка» ───────────────────────────────────────────────────

  renderRequestTab(container) {
    container.innerHTML = `
      <div class="transfers-card">
        <div class="kind-toggle-row">
          <button class="kind-toggle-btn ${this.requestKind === 'deal' ? 'active' : ''}" data-kind="deal" type="button">🤝 Сделка</button>
          <button class="kind-toggle-btn ${this.requestKind === 'surcharge' ? 'active' : ''}" data-kind="surcharge" type="button">⚡ Спешл</button>
          <button class="kind-toggle-btn ${this.requestKind === 'urn_sale' ? 'active' : ''}" data-kind="urn_sale" type="button">🗑 Урна</button>
        </div>

        <form id="transfer-request-form">
          <div id="transfer-form-fields"></div>

          <!-- Фото к заявке -->
          <div class="form-group" id="transfer-photo-group" style="margin-top: 14px;">
            <label class="form-label">Скриншот / фото (необязательно)</label>
            <div class="photo-upload-zone" id="photo-dropzone">
              <span id="photo-upload-prompt">📷 Нажмите, чтобы прикрепить фото</span>
              <input type="file" id="transfer-photo-input" accept="image/jpeg,image/png,image/webp">
              <div id="photo-preview-container" style="display: none;"></div>
            </div>
          </div>

          <div id="form-preview-box" class="preview-box" style="display: none;"></div>

          <button class="preview-request-btn" id="btn-preview-transfer" type="button">
            🔍 Проверить заявку
          </button>

          <div id="form-error-box" style="display: none; color: var(--color-danger); font-size: 0.82rem; margin: 10px 0; background: rgba(239,68,68,0.1); padding: 8px 12px; border-radius: 8px;"></div>

          <button class="submit-request-btn" id="btn-submit-transfer" type="submit">
            Отправить заявку
          </button>
        </form>
      </div>
    `;

    this.renderFormFields();
    this.bindRequestForm(container);
  }

  // Обмен «игрок на игрока»: у каждой половины своя цена — разница и есть доплата.
  swapFieldsHtml() {
    const half = (prefix, title, hint) => `
      <div style="font-size: 0.8rem; font-weight: 700; margin: 6px 0 4px;">${title}</div>
      <div class="form-group">
        <input type="text" id="${prefix}-player" class="form-input" placeholder="${hint}" required>
      </div>
      <div style="display: flex; gap: 10px;">
        <div class="form-group" style="flex: 1;">
          <input type="number" id="${prefix}-ovr" class="form-input" placeholder="OVR" min="1" max="199" required>
        </div>
        <div class="form-group" style="flex: 1.2;">
          <input type="text" id="${prefix}-price" class="form-input" placeholder="Цена, млн" required>
        </div>
      </div>`;
    return `
      <div style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 8px; line-height: 1.4;">
        Две сделки одной заявкой: вторая сторона принимает или отклоняет обмен целиком, ответственный решает обе вместе.
      </div>
      ${half('swap-give', '➡️ Отдаю', 'Игрок вашего состава')}
      ${half('swap-get', '⬅️ Получаю', 'Игрок клуба-партнёра')}`;
  }

  renderFormFields() {
    const photoGroup = document.getElementById('transfer-photo-group');
    if (photoGroup) photoGroup.style.display = (this.requestKind === 'deal' && this.dealRole === 'swap') ? 'none' : '';
    const fieldsContainer = document.getElementById('transfer-form-fields');
    if (!fieldsContainer) return;

    if (this.requestKind === 'deal') {
      const lot = this.respondLot && this.respondLot.role === this.dealRole ? this.respondLot : null;
      fieldsContainer.innerHTML = `
        ${lot ? `
        <div class="board-respond-banner">
          <span>📌 Отклик на лот #${lot.id}: ${escapeHtml(lot.label)}</span>
          <button type="button" class="board-respond-clear" id="board-respond-clear" title="Без привязки к лоту">✕</button>
        </div>` : ''}
        <div class="role-toggle-row">
          <button class="role-toggle-btn ${this.dealRole === 'buy' ? 'active buy' : ''}" data-role="buy" type="button">🟢 Я покупаю игрока</button>
          <button class="role-toggle-btn ${this.dealRole === 'sell' ? 'active sell' : ''}" data-role="sell" type="button">🔴 Я продаю игрока</button>
          <button class="role-toggle-btn ${this.dealRole === 'swap' ? 'active swap' : ''}" data-role="swap" type="button">🔁 Обмен</button>
        </div>

        <div class="form-group">
          <label class="form-label" for="deal-other-club">${{ buy: 'У какого клуба покупаете', sell: 'Какому клубу продаёте', swap: 'С каким клубом меняетесь' }[this.dealRole]}</label>
          <input type="text" id="deal-other-club" class="form-input" placeholder="Название клуба соперника" required>
        </div>

        ${this.dealRole === 'swap' ? this.swapFieldsHtml() : `
        <div class="form-group">
          <label class="form-label" for="deal-player">Имя футболиста (карточки)</label>
          <input type="text" id="deal-player" class="form-input" placeholder="Например: K. De Bruyne" required>
        </div>

        <div style="display: flex; gap: 10px;">
          <div class="form-group" style="flex: 1;">
            <label class="form-label" for="deal-ovr">OVR карты</label>
            <input type="number" id="deal-ovr" class="form-input" placeholder="105" min="1" max="199" required>
          </div>
          <div class="form-group" style="flex: 1.2;">
            <label class="form-label" for="deal-price">Сумма (в млн)</label>
            <input type="text" id="deal-price" class="form-input" placeholder="12.5" required>
          </div>
        </div>`}
      `;
    } else if (this.requestKind === 'surcharge') {
      fieldsContainer.innerHTML = `
        <div style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 12px; line-height: 1.4;">
          Доплата за спешл списывается из бюджета клуба по таблице окна. Слот покупки не тратится.
        </div>
        <div class="form-group">
          <label class="form-label" for="surcharge-player">Игрок вашего состава</label>
          <input type="text" id="surcharge-player" class="form-input" placeholder="Имя игрока" required>
        </div>
        <div class="form-group">
          <label class="form-label" for="surcharge-ovr">Новый OVR (спешл)</label>
          <input type="number" id="surcharge-ovr" class="form-input" placeholder="105" min="100" max="199" required>
        </div>
      `;
    } else if (this.requestKind === 'urn_sale') {
      fieldsContainer.innerHTML = `
        <div style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 12px; line-height: 1.4;">
          Продажа в урну: выплата составит <b>(TM + Спешл) / 2</b> (или / 3 для непродаваемой). Тратит 1 слот продажи.
        </div>
        <div class="form-group">
          <label class="form-label" for="urn-player">Игрок вашего состава</label>
          <input type="text" id="urn-player" class="form-input" placeholder="Имя игрока" required>
        </div>
        <div style="display: flex; gap: 10px;">
          <div class="form-group" style="flex: 1;">
            <label class="form-label" for="urn-tm">Цена по TM (млн)</label>
            <input type="text" id="urn-tm" class="form-input" placeholder="15" required>
          </div>
          <div class="form-group" style="flex: 1;">
            <label class="form-label" for="urn-special">Цена спешл (млн)</label>
            <input type="text" id="urn-special" class="form-input" placeholder="5" required>
          </div>
        </div>
        <div class="form-group" style="margin-top: 8px;">
          <label style="display: flex; align-items: center; gap: 8px; color: #fff; font-size: 0.85rem; cursor: pointer;">
            <input type="checkbox" id="urn-sellable" checked>
            <span>Карта продаваемая на рынке FC</span>
          </label>
        </div>
      `;
    }

    if (this.requestKind === 'deal' && this.prefill) {
      const p = this.prefill;
      this.prefill = null;
      const set = (id, value) => {
        const el = document.getElementById(id);
        if (el && value != null && value !== '') el.value = value;
      };
      set('deal-other-club', p.club);
      set('deal-player', p.player);
      set('deal-ovr', p.ovr);
      set('deal-price', p.price);
    }

    document.getElementById('board-respond-clear')?.addEventListener('click', () => {
      this.respondLot = null;
      this.renderFormFields();
    });

    // Role toggle bindings
    fieldsContainer.querySelectorAll('.role-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.dealRole = btn.dataset.role;
        this.renderFormFields();
      });
    });

    this.bindSuggest();
  }

  // Поля формы как для предпроверки: тот же набор, что уходит при подаче.
  collectRequestFields() {
    const v = (id) => document.getElementById(id)?.value ?? '';
    if (this.requestKind === 'deal' && this.dealRole === 'swap') {
      return {
        kind: 'swap', other_club: v('deal-other-club'),
        give_player: v('swap-give-player'), give_ovr: v('swap-give-ovr'), give_price: v('swap-give-price'),
        get_player: v('swap-get-player'), get_ovr: v('swap-get-ovr'), get_price: v('swap-get-price'),
      };
    }
    if (this.requestKind === 'deal') {
      return {
        kind: 'deal', role: this.dealRole, other_club: v('deal-other-club'),
        player: v('deal-player'), ovr: v('deal-ovr'), price: v('deal-price'),
      };
    }
    if (this.requestKind === 'surcharge') {
      return { kind: 'surcharge', player: v('surcharge-player'), ovr: v('surcharge-ovr') };
    }
    return {
      kind: 'urn_sale', player: v('urn-player'), tm_price: v('urn-tm'), special_price: v('urn-special'),
      sellable: document.getElementById('urn-sellable')?.checked ? '1' : '0',
    };
  }

  // «Проверить заявку»: сервер прогоняет настоящие правила подачи, ничего не записывая.
  async runPreview() {
    const box = document.getElementById('form-preview-box');
    const btn = document.getElementById('btn-preview-transfer');
    if (!box || !btn || this.previewing) return;
    this.previewing = true;
    btn.disabled = true;
    box.style.display = 'block';
    box.className = 'preview-box';
    box.textContent = 'Проверяем…';
    try {
      const res = await api.previewTransfer(this.collectRequestFields());
      const d = res.data || {};
      const lines = [];
      (d.blocks || []).forEach(t => lines.push(`<div class="preview-line block">⛔ ${escapeHtml(t)}</div>`));
      (d.warnings || []).forEach(t => lines.push(`<div class="preview-line warn">⚠️ ${escapeHtml(t)}</div>`));
      if (d.ok) {
        const price = d.price ? ` Сумма по заявке: <b>${escapeHtml(String(d.price))}</b>.` : '';
        lines.unshift(`<div class="preview-line ok">✅ ${(d.warnings || []).length
          ? 'Подать можно, ответственный увидит предупреждения.'
          : 'Всё в порядке, заявку можно подавать.'}${price}</div>`);
      }
      box.className = `preview-box ${d.ok ? ((d.warnings || []).length ? 'warn' : 'ok') : 'block'}`;
      box.innerHTML = lines.join('');
    } catch (err) {
      box.className = 'preview-box block';
      box.textContent = err.message || 'Не удалось проверить заявку';
    } finally {
      this.previewing = false;
      btn.disabled = false;
    }
  }

  // Автоподбор имени клуба/игрока: подсказки с сервера, выбор подставляет каноничное имя.
  bindSuggest() {
    const val = (id) => (document.getElementById(id)?.value || '').trim();
    if (this.requestKind === 'deal') {
      this.attachSuggest('deal-other-club', (q) => api.getTransferSuggest('club', q), () => {
        const next = this.dealRole === 'swap'
          ? (document.getElementById('swap-give-player') || document.getElementById('swap-get-player'))
          : document.getElementById('deal-player');
        next?.focus();
      });
      this.attachSuggest('deal-player', (q) => {
        if (this.dealRole === 'sell') {
          return api.getTransferSuggest('player', q, { own: true });
        }
        const otherClub = val('deal-other-club');
        if (!otherClub) {
          return Promise.resolve({ status: 'ok', data: [] });
        }
        return api.getTransferSuggest('player', q, { club: otherClub });
      }, null, 'deal-ovr');
      this.attachSuggest('swap-give-player', (q) => api.getTransferSuggest('player', q, { own: true }), null, 'swap-give-ovr');
      this.attachSuggest('swap-get-player', (q) => {
        const otherClub = val('deal-other-club');
        if (!otherClub) {
          return Promise.resolve({ status: 'ok', data: [] });
        }
        return api.getTransferSuggest('player', q, { club: otherClub });
      }, null, 'swap-get-ovr');
    } else if (this.requestKind === 'surcharge') {
      this.attachSuggest('surcharge-player', (q) => api.getTransferSuggest('player', q, { own: true }), null, 'surcharge-ovr');
    } else if (this.requestKind === 'urn_sale') {
      this.attachSuggest('urn-player', (q) => api.getTransferSuggest('player', q, { own: true }));
    }
  }

  // Версии карточки игрока (Renderz): одна — подставляем её OVR, несколько — подставляем высшую
  // и показываем под полем OVR кнопки версий, чтобы выбрать нужную.
  fillCardOvr(ovrId, cards) {
    const ovr = document.getElementById(ovrId);
    if (!ovr) return;
    ovr.parentElement.querySelector('.card-versions')?.remove();
    if (!cards || !cards.length) return;
    const apply = (value) => {
      ovr.value = value;
      ovr.dispatchEvent(new Event('input', { bubbles: true }));
    };
    apply(cards[0].ovr);
    if (cards.length < 2) return;
    const row = document.createElement('div');
    row.className = 'card-versions';
    row.innerHTML = '<span class="card-versions-label">Версии карты:</span>' + cards.map((c, i) =>
      `<button type="button" class="card-version${i === 0 ? ' active' : ''}" data-ovr="${c.ovr}"
        ${c.program ? `title="${escapeHtml(c.program)}"` : ''}>${c.ovr}${c.tradable ? '' : ' · не прод.'}</button>`).join('');
    row.querySelectorAll('.card-version').forEach(btn => btn.addEventListener('click', () => {
      row.querySelectorAll('.card-version').forEach(b => b.classList.toggle('active', b === btn));
      apply(btn.dataset.ovr);
    }));
    ovr.insertAdjacentElement('afterend', row);
  }

  attachSuggest(inputId, fetcher, onPick, ovrId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.setAttribute('autocomplete', 'off');
    const box = document.createElement('div');
    box.className = 'suggest-list';
    box.hidden = true;
    input.parentElement.classList.add('suggest-host');
    input.insertAdjacentElement('afterend', box);

    let timer = null;
    let seq = 0;
    const hide = () => { box.hidden = true; box.innerHTML = ''; };

    const show = (items) => {
      if (!items.length) return hide();
      box.innerHTML = items.map((it, i) => `
        <button type="button" class="suggest-item" data-i="${i}">
          <span class="suggest-name">${escapeHtml(it.name)}</span>
          ${it.cards && it.cards.length ? `<span class="suggest-ovr">${it.cards.map(c => c.ovr).join(' / ')}</span>` : ''}
          ${it.club ? `<span class="suggest-club">${escapeHtml(it.club)}</span>` : ''}
        </button>`).join('');
      box.hidden = false;
      box.querySelectorAll('.suggest-item').forEach(btn => {
        // pointerdown, а не click: к click поле уже теряет фокус и список прячется
        btn.addEventListener('pointerdown', (e) => {
          e.preventDefault();
          const item = items[Number(btn.dataset.i)];
          input.value = item.name;
          hide();
          if (ovrId) this.fillCardOvr(ovrId, item.cards);
          if (onPick) onPick(item);
        });
      });
    };

    input.addEventListener('input', () => {
      clearTimeout(timer);
      // другое имя — прежние версии карты больше не относятся к игроку
      if (ovrId) document.getElementById(ovrId)?.parentElement.querySelector('.card-versions')?.remove();
      const q = input.value.trim();
      if (!q) return hide();
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const res = await fetcher(q);
          if (mine === seq && res.status === 'ok' && document.activeElement === input) show(res.data || []);
        } catch (_) { /* подсказки необязательны: ввод работает и без них */ }
      }, 180);
    });
    input.addEventListener('blur', () => { seq++; setTimeout(hide, 120); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  }

  bindRequestForm(container) {
    // Kind buttons
    container.querySelectorAll('.kind-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.requestKind = btn.dataset.kind;
        container.querySelectorAll('.kind-toggle-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.kind === this.requestKind);
        });
        this.renderFormFields();
      });
    });

    // Photo input
    const dropzone = document.getElementById('photo-dropzone');
    const fileInput = document.getElementById('transfer-photo-input');
    const previewContainer = document.getElementById('photo-preview-container');
    const promptSpan = document.getElementById('photo-upload-prompt');

    dropzone?.addEventListener('click', (e) => {
      if (e.target.closest('.photo-remove-btn')) return;
      fileInput.click();
    });

    fileInput?.addEventListener('change', () => {
      const file = fileInput.files?.[0];
      if (!file) return;
      this.selectedPhotoFile = file;
      const reader = new FileReader();
      reader.onload = (e) => {
        promptSpan.style.display = 'none';
        previewContainer.style.display = 'block';
        previewContainer.innerHTML = `
          <div class="photo-preview-box">
            <img src="${e.target.result}" class="photo-preview-img" alt="Превью фото">
            <button class="photo-remove-btn" type="button" title="Удалить">✕</button>
          </div>
        `;
        previewContainer.querySelector('.photo-remove-btn')?.addEventListener('click', (ev) => {
          ev.stopPropagation();
          this.selectedPhotoFile = null;
          fileInput.value = '';
          previewContainer.style.display = 'none';
          promptSpan.style.display = 'inline';
        });
      };
      reader.readAsDataURL(file);
    });

    document.getElementById('btn-preview-transfer')?.addEventListener('click', () => this.runPreview());
    // Любая правка полей делает прошлый результат неактуальным.
    const resetPreview = () => {
      const box = document.getElementById('form-preview-box');
      if (box) box.style.display = 'none';
    };
    document.getElementById('transfer-form-fields')?.addEventListener('input', resetPreview);
    container.querySelectorAll('.kind-toggle-btn').forEach(b => b.addEventListener('click', resetPreview));

    // Submit
    const form = document.getElementById('transfer-request-form');
    const errorBox = document.getElementById('form-error-box');
    const submitBtn = document.getElementById('btn-submit-transfer');

    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (this.submitting) return;

      errorBox.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Отправка...';
      this.submitting = true;

      try {
        const formData = new FormData();
        if (this.selectedPhotoFile) {
          formData.append('photo', this.selectedPhotoFile);
        }

        if (this.requestKind === 'deal' && this.dealRole === 'swap') {
          const fv = (id) => document.getElementById(id).value;
          formData.append('other_club', fv('deal-other-club'));
          formData.append('give_player', fv('swap-give-player'));
          formData.append('give_ovr', fv('swap-give-ovr'));
          formData.append('give_price', fv('swap-give-price'));
          formData.append('get_player', fv('swap-get-player'));
          formData.append('get_ovr', fv('swap-get-ovr'));
          formData.append('get_price', fv('swap-get-price'));
          await api.createTransferSwap(formData, true);
        } else if (this.requestKind === 'deal') {
          formData.append('role', this.dealRole);
          formData.append('other_club', document.getElementById('deal-other-club').value);
          formData.append('player', document.getElementById('deal-player').value);
          formData.append('ovr', document.getElementById('deal-ovr').value);
          formData.append('price', document.getElementById('deal-price').value);
          if (this.respondLot && this.respondLot.role === this.dealRole) {
            formData.append('lot_id', String(this.respondLot.id));
          }
          await api.createTransferDeal(formData, true);
        } else if (this.requestKind === 'surcharge') {
          formData.append('player', document.getElementById('surcharge-player').value);
          formData.append('ovr', document.getElementById('surcharge-ovr').value);
          await api.createTransferSurcharge(formData, true);
        } else if (this.requestKind === 'urn_sale') {
          formData.append('player', document.getElementById('urn-player').value);
          formData.append('tm_price', document.getElementById('urn-tm').value);
          formData.append('special_price', document.getElementById('urn-special').value);
          formData.append('sellable', document.getElementById('urn-sellable').checked ? '1' : '0');
          await api.createTransferUrnSale(formData, true);
        }

        tgBridge.hapticImpact('heavy');
        alert('Заявка успешно подана!');
        this.selectedPhotoFile = null;
        this.respondLot = null;
        this.boardData = null;
        this.statusData = null; // force reload status
        this.activeTab = 'status';
        this.init();
      } catch (err) {
        errorBox.textContent = err.message || 'Ошибка отправки заявки';
        errorBox.style.display = 'block';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Отправить заявку';
      } finally {
        this.submitting = false;
      }
    });
  }

  // ─── 3. Вкладка «Рынок» ────────────────────────────────────────────────────

  async loadMarket(container, force = false) {
    if (!this.marketData || force) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">⏳</div>
          <div>Загрузка рынка...</div>
        </div>`;
      try {
        const res = await api.getTransferMarket(this.marketFilters);
        if (res.status !== 'ok') {
          container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(res.message || 'Ошибка')}</div>`;
          return;
        }
        this.marketData = res.data;
      } catch (err) {
        container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(err.message || 'Ошибка сети')}</div>`;
        return;
      }
    }

    const f = this.marketFilters;
    const { urn, catalog, catalog_total, clubs, can_buy } = this.marketData;
    const sorts = [['ovr', 'По OVR'], ['price', 'Дешевле'], ['price_desc', 'Дороже'], ['name', 'По имени']];

    container.innerHTML = `
      <form class="market-filters" id="market-filters">
        <input type="search" id="market-q" class="form-input" placeholder="Поиск игрока" value="${escapeHtml(f.q)}" autocomplete="off">
        <div class="market-filter-row">
          <input type="number" id="market-ovr-min" class="form-input" placeholder="OVR от" min="1" max="199" value="${escapeHtml(String(f.ovrMin))}">
          <input type="number" id="market-ovr-max" class="form-input" placeholder="OVR до" min="1" max="199" value="${escapeHtml(String(f.ovrMax))}">
        </div>
        <div class="market-filter-row">
          <select id="market-club" class="form-input">
            <option value="">Все клубы</option>
            ${(clubs || []).map(c => `<option value="${escapeHtml(c)}" ${c === f.club ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}
          </select>
          <select id="market-sort" class="form-input">
            ${sorts.map(([v, t]) => `<option value="${v}" ${v === f.sort ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
        </div>
        <button class="market-apply-btn" type="submit">Применить</button>
      </form>

      <div class="market-section-title">🗑 В урне <span class="market-count">${urn.length}</span></div>
      ${urn.length ? urn.map(it => this.renderUrnCard(it, can_buy)).join('') : `
        <div class="market-empty">Карточек на выкуп нет.</div>`}

      <div class="market-section-title">📇 Каталог игроков <span class="market-count">${catalog_total}</span></div>
      ${catalog.length ? catalog.map((it, i) => this.renderCatalogCard(it, i)).join('') : `
        <div class="market-empty">Ничего не найдено. Справочник пополняется по мере одобренных сделок.</div>`}
      ${catalog_total > catalog.length ? `<div class="market-more">Показано ${catalog.length} из ${catalog_total} — уточните поиск.</div>` : ''}
    `;

    document.getElementById('market-filters').addEventListener('submit', (e) => {
      e.preventDefault();
      this.marketFilters = {
        q: document.getElementById('market-q').value.trim(),
        ovrMin: document.getElementById('market-ovr-min').value,
        ovrMax: document.getElementById('market-ovr-max').value,
        club: document.getElementById('market-club').value,
        sort: document.getElementById('market-sort').value,
      };
      tgBridge.hapticImpact('light');
      this.loadMarket(container, true);
    });

    container.querySelectorAll('.btn-buyout').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Выкупить этого игрока из урны?')) return;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          await api.createTransferUrnBuy(btn.dataset.id);
          tgBridge.hapticImpact('heavy');
          alert('Заявка на выкуп подана!');
          this.marketData = null;
          this.statusData = null;
          this.activeTab = 'status';
          this.init();
        } catch (err) {
          alert(err.message || 'Ошибка выкупа');
          btn.disabled = false;
          btn.textContent = 'Выкупить';
        }
      });
    });

    container.querySelectorAll('.btn-offer').forEach(btn => {
      btn.addEventListener('click', () => {
        const item = catalog[Number(btn.dataset.idx)];
        if (!item) return;
        tgBridge.hapticImpact('light');
        this.prefill = {
          club: item.club || '',
          player: item.player_name,
          ovr: item.ovr || '',
          price: item.price_k ? String(item.price_k / 1000) : '',
        };
        this.requestKind = 'deal';
        this.dealRole = 'buy';
        this.activeTab = 'request';
        this.renderShell();
        this.loadActiveTab();
      });
    });
  }

  renderUrnCard(it, canBuy) {
    return `
      <div class="urn-item-card" data-id="${it.id}">
        <div class="urn-item-info">
          <div class="urn-item-name">${escapeHtml(it.player_name)} ${it.ovr ? `<span class="req-ovr-tag">OVR ${it.ovr}</span>` : ''}</div>
          <div class="urn-item-club">Из клуба: ${it.from_club ? this.renderRouteClub(it.from_club) : '<b>—</b>'}</div>
          <div class="urn-item-price">Цена выкупа: <b>${it.buy_price}</b></div>
        </div>
        <div>
          ${canBuy ? `<button class="btn-buyout" data-id="${it.id}" type="button">Выкупить</button>` : '<span style="font-size: 0.72rem; color: var(--text-muted);">Недоступно</span>'}
        </div>
      </div>`;
  }

  renderCatalogCard(it, idx) {
    return `
      <div class="urn-item-card ${it.banned ? 'is-banned' : ''}">
        <div class="urn-item-info">
          <div class="urn-item-name">${escapeHtml(it.player_name)} ${it.ovr ? `<span class="req-ovr-tag">OVR ${it.ovr}</span>` : ''}</div>
          <div class="urn-item-club">${it.club ? this.renderRouteClub(it.club) : '<b>—</b>'}</div>
          <div class="urn-item-price">${it.price ? `Последняя цена: <b>${escapeHtml(String(it.price))}</b>` : 'Цена неизвестна'}</div>
          ${it.banned ? `<div class="market-ban">⛔ ${escapeHtml(it.ban_reason || 'В списке запрещённых')}</div>` : ''}
        </div>
        <div>
          <button class="btn-offer" data-idx="${idx}" type="button">Предложить</button>
        </div>
      </div>`;
  }

  // ─── 4. Вкладка «Доска» ────────────────────────────────────────────────────

  async loadBoard(container, force = false) {
    if (!this.boardData || force) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">⏳</div>
          <div>Загрузка доски...</div>
        </div>`;
      try {
        const res = await api.getTransferBoard();
        if (res.status !== 'ok') {
          container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(res.message || 'Ошибка')}</div>`;
          return;
        }
        this.boardData = res.data;
      } catch (err) {
        container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(err.message || 'Ошибка сети')}</div>`;
        return;
      }
    }

    const d = this.boardData;
    if (!d.open) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">📌</div>
          <div>Доска работает, пока трансферное окно открыто.</div>
        </div>`;
      return;
    }
    const lots = d.lots || [];
    const sell = this.boardSide === 'sell';
    container.innerHTML = `
      ${d.can_post ? `
      <form class="transfers-card board-form" id="board-form">
        <div class="role-toggle-row">
          <button class="role-toggle-btn ${sell ? 'active sell' : ''}" data-side="sell" type="button">🔴 Продаю</button>
          <button class="role-toggle-btn ${!sell ? 'active buy' : ''}" data-side="buy" type="button">🟢 Ищу</button>
        </div>
        <div class="form-group">
          <input type="text" id="board-player" class="form-input" placeholder="${sell ? 'Игрок вашего состава' : 'Игрок (необязательно)'}" ${sell ? 'required' : ''}>
        </div>
        <div class="market-filter-row">
          <input type="number" id="board-ovr" class="form-input" placeholder="OVR" min="1" max="199">
          <input type="text" id="board-price" class="form-input" placeholder="${sell ? 'Цена, млн' : 'Бюджет, млн'}">
        </div>
        <div class="form-group" style="margin-top: 8px;">
          <input type="text" id="board-note" class="form-input" maxlength="200" placeholder="${sell ? 'Комментарий (необязательно)' : 'Кого ищете: позиция, OVR, бюджет'}">
        </div>
        <div id="board-error" class="board-error" style="display: none;"></div>
        <button class="submit-request-btn" id="board-submit" type="submit">Повесить лот</button>
        <div class="board-hint">Лотов клуба: ${d.my_open} из ${d.max_open}. Лот — объявление: деньги и слоты не занимает.</div>
      </form>` : `
      <div class="board-hint">${d.club
        ? (d.my_open >= d.max_open ? `У клуба уже ${d.max_open} лота — снимите один, чтобы повесить новый.` : 'Вешать лоты сейчас нельзя.')
        : 'Лоты вешают тренеры клубов.'}</div>`}

      <div class="market-section-title">📌 На доске <span class="market-count">${lots.length}</span></div>
      ${lots.length ? lots.map((lot, i) => this.renderLotCard(lot, i)).join('') : `
        <div class="market-empty">Лотов пока нет — повесьте первый.</div>`}
    `;

    container.querySelectorAll('#board-form .role-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.boardSide = btn.dataset.side;
        this.loadBoard(container);
      });
    });
    if (d.can_post && sell) {
      this.attachSuggest('board-player', (q) => api.getTransferSuggest('player', q, { own: true }));
    }

    document.getElementById('board-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('board-submit');
      const errorBox = document.getElementById('board-error');
      const v = (id) => document.getElementById(id).value;
      btn.disabled = true;
      errorBox.style.display = 'none';
      try {
        await api.createTransferLot({
          side: this.boardSide, player: v('board-player'), ovr: v('board-ovr'),
          price: v('board-price'), note: v('board-note'),
        });
        tgBridge.hapticImpact('heavy');
        this.loadBoard(container, true);
      } catch (err) {
        errorBox.textContent = err.message || 'Не удалось повесить лот';
        errorBox.style.display = 'block';
        btn.disabled = false;
      }
    });

    container.querySelectorAll('.btn-lot-close').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Снять лот с доски?')) return;
        btn.disabled = true;
        try {
          await api.closeTransferLot(btn.dataset.id);
          tgBridge.hapticImpact('light');
          this.loadBoard(container, true);
        } catch (err) {
          alert(err.message || 'Не удалось снять лот');
          btn.disabled = false;
        }
      });
    });

    container.querySelectorAll('.btn-lot-respond').forEach(btn => {
      btn.addEventListener('click', () => {
        const lot = lots[Number(btn.dataset.idx)];
        if (!lot) return;
        tgBridge.hapticImpact('light');
        // Продают — отвечаем покупкой этого игрока; ищут — продажей своего игрока клубу лота.
        const role = lot.side === 'sell' ? 'buy' : 'sell';
        const what = lot.player ? `${lot.player}${lot.ovr ? ` (OVR ${lot.ovr})` : ''}` : (lot.note || '');
        this.respondLot = { id: lot.id, role, label: `${lot.club} — ${lot.side_label.toLowerCase()} ${what}`.trim() };
        this.prefill = role === 'buy'
          ? { club: lot.club, player: lot.player, ovr: lot.ovr || '', price: lot.price_k ? String(lot.price_k / 1000) : '' }
          : { club: lot.club, player: '', ovr: '', price: lot.price_k ? String(lot.price_k / 1000) : '' };
        this.requestKind = 'deal';
        this.dealRole = role;
        this.activeTab = 'request';
        this.renderShell();
        this.loadActiveTab();
      });
    });
  }

  renderLotCard(lot, idx) {
    const sell = lot.side === 'sell';
    const pending = lot.responses?.pending || 0;
    let action = '';
    if (lot.mine) {
      action = `<button class="btn-offer btn-lot-close" data-id="${lot.id}" type="button">Снять</button>`;
    } else if (lot.can_respond) {
      action = `<button class="btn-offer btn-lot-respond" data-idx="${idx}" type="button">${sell ? 'Купить' : 'Предложить'}</button>`;
    }
    return `
      <div class="urn-item-card board-lot ${sell ? 'is-sell' : 'is-buy'} ${lot.mine ? 'is-mine' : ''}">
        <div class="urn-item-info">
          <div class="urn-item-name">
            <span class="board-side ${sell ? 'sell' : 'buy'}">${escapeHtml(lot.side_label)}</span>
            ${lot.player ? escapeHtml(lot.player) : ''} ${lot.ovr ? `<span class="req-ovr-tag">OVR ${lot.ovr}</span>` : ''}
          </div>
          <div class="urn-item-club">${this.renderRouteClub(lot.club)}</div>
          ${lot.price ? `<div class="urn-item-price">${sell ? 'Цена' : 'Бюджет'}: <b>${escapeHtml(String(lot.price))}</b></div>` : ''}
          ${lot.note ? `<div class="board-note">${escapeHtml(lot.note)}</div>` : ''}
          ${pending ? `<div class="board-note">Откликов в работе: ${pending}</div>` : ''}
        </div>
        <div>${action}</div>
      </div>`;
  }

  // ─── 5. Вкладка «История» ──────────────────────────────────────────────────

  async loadHistory(container, force = false) {
    const filters = this.historyFilters || (this.historyFilters = { windowId: null, mine: false, club: '' });
    if (!this.historyData || force) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">⏳</div>
          <div>Загрузка истории...</div>
        </div>`;
      try {
        const res = await api.getTransferHistory(filters);
        if (res.status === 'ok') {
          this.historyData = res.data;
        } else {
          container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(res.message || 'Ошибка')}</div>`;
          return;
        }
      } catch (err) {
        container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(err.message || 'Ошибка сети')}</div>`;
        return;
      }
    }

    const data = this.historyData;
    const items = data.items || [];
    const windows = data.windows || [];
    const clubs = data.clubs || [];
    const currentId = data.window ? data.window.id : null;

    const windowOptions = windows.map(w => {
      const label = w.title ? w.title : `Окно #${w.id}`;
      return `<option value="${w.id}" ${w.id === currentId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    }).join('');
    const clubOptions = ['<option value="">Все клубы</option>'].concat(clubs.map(c =>
      `<option value="${escapeHtml(c)}" ${c === data.club ? 'selected' : ''}>${escapeHtml(c)}</option>`)).join('');

    const toolbar = `
      <div class="history-toolbar">
        <div class="history-scope">
          <button class="transfers-tab-btn ${data.mine ? '' : 'active'}" data-mine="0" type="button">Все</button>
          <button class="transfers-tab-btn ${data.mine ? 'active' : ''}" data-mine="1" type="button">Мои</button>
        </div>
        ${windows.length > 1 ? `<select class="history-select" id="history-window">${windowOptions}</select>` : ''}
        ${clubs.length > 0 || data.club ? `<select class="history-select" id="history-club">${clubOptions}</select>` : ''}
      </div>`;

    let body;
    if (items.length === 0) {
      body = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">📜</div>
          <div style="font-weight: 700; color: #fff; margin-bottom: 4px;">История пуста</div>
          <div style="font-size: 0.8rem;">${data.mine
            ? 'В этом окне у вас и вашего клуба нет заявок.'
            : 'В этом окне ещё нет завершённых трансферов.'}</div>
        </div>`;
    } else {
      body = `
        <div style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 12px;">
          ${data.mine ? 'Заявки вашего клуба и ваши' : 'Одобренные трансферы лиги'} (${items.length}):
        </div>
        ${items.map(it => this.renderHistoryItem(it, data.mine)).join('')}`;
    }

    container.innerHTML = toolbar + body;

    container.querySelectorAll('.history-scope [data-mine]').forEach(btn => {
      btn.addEventListener('click', () => {
        filters.mine = btn.dataset.mine === '1';
        filters.club = '';
        this.loadHistory(container, true);
      });
    });
    const windowSel = container.querySelector('#history-window');
    if (windowSel) {
      windowSel.addEventListener('change', () => {
        filters.windowId = Number(windowSel.value) || null;
        filters.club = '';
        this.loadHistory(container, true);
      });
    }
    const clubSel = container.querySelector('#history-club');
    if (clubSel) {
      clubSel.addEventListener('change', () => {
        filters.club = clubSel.value;
        this.loadHistory(container, true);
      });
    }
    container.querySelectorAll('.btn-view-photo').forEach(btn => {
      btn.addEventListener('click', () => {
        this.openPhotoModal(btn.dataset.id);
      });
    });
  }

  /** Клуб в маршруте сделки: герб (если найден) + название. «Урна» — без герба, с 🗑. */
  renderRouteClub(name) {
    if (!name) return '';
    const isUrn = String(name).trim().toLowerCase() === 'урна';
    const url = isUrn ? null : getTeamLogoUrl(name);
    const mark = isUrn
      ? '<span class="hist-club-urn">🗑</span>'
      : (url ? `<img class="hist-club-logo" src="${escapeHtml(url)}" alt="" loading="lazy" decoding="async" onerror="this.remove()" />` : '');
    return `<span class="hist-club">${mark}<b>${escapeHtml(name)}</b></span>`;
  }

  renderHistoryItem(it, mine) {
    const kindLabel = {
      deal: '🤝 Сделка',
      surcharge: '⚡ Доплата',
      urn_sale: '🗑 Продажа в урну',
      urn_buy: '🛍 Выкуп из урны',
      free_agent: '🏃 СА',
    }[it.kind] || it.kind;
    const statusMap = {
      pending_counterparty: { label: 'Ждёт стороны', cls: 'badge-draft' },
      pending_manager: { label: 'На рассмотрении', cls: 'badge-draft' },
      approved: { label: 'Одобрен', cls: 'badge-open' },
      rejected: { label: 'Отклонён', cls: 'badge-closed' },
      withdrawn: { label: 'Отозван', cls: 'badge-closed' },
      cancelled: { label: 'Отменён', cls: 'badge-closed' },
    };
    const st = mine ? (statusMap[it.status] || { label: it.status, cls: 'badge-closed' }) : statusMap.approved;

    const initial = escapeHtml(String(it.player_name || '?').trim().charAt(0).toUpperCase() || '?');
    const fallback = `<span class="hist-portrait-fallback" ${it.portrait_url ? 'hidden' : ''}>${initial}</span>`;
    const isRenderz = Boolean(it.portrait_url && it.portrait_url.includes('renderz'));
    const portrait = (it.portrait_url
      ? `<img class="hist-portrait-img${isRenderz ? ' is-renderz' : ''}" src="${escapeHtml(it.portrait_url)}" alt="" loading="lazy"
              onerror="this.hidden=true;this.nextElementSibling.hidden=false" />`
      : '') + fallback;

    return `
      <div class="req-item req-item-hist">
        <div class="hist-portrait">
          <div class="hist-portrait-frame">
            ${portrait}
          </div>
          ${it.ovr ? `<span class="hist-portrait-ovr">${it.ovr}</span>` : ''}
        </div>
        <div class="hist-body">
          <div class="req-item-head">
            <span class="req-item-title">
              <span class="hist-kind">${it.swap_partner_id ? '🔁 Обмен' : kindLabel}</span>
              <b>${escapeHtml(it.player_name)}</b>
            </span>
            <span class="window-status-badge ${st.cls}">${st.label}</span>
          </div>
          <div class="req-route hist-route">
            ${it.from_club ? this.renderRouteClub(it.from_club) : ''}
            ${it.from_club && it.to_club ? '<span class="hist-arrow">→</span>' : ''}
            ${it.to_club ? this.renderRouteClub(it.to_club) : ''}
          </div>
          ${mine && it.decided_reason ? `
            <div style="font-size: 0.74rem; color: var(--color-danger); margin: 4px 0;">
              Причина: ${escapeHtml(it.decided_reason)}
            </div>` : ''}
          <div class="req-footer">
            <div class="req-price">
              ${it.price}
              ${it.special_price_k ? `<span class="hist-price-detail">(${it.tm_price_k ? (it.tm_price_k / 1000).toFixed(0) : ((it.price_k - it.special_price_k) / 1000).toFixed(0)} + ${(it.special_price_k / 1000).toFixed(0)} спешл)</span>` : ''}
            </div>
            ${it.has_photo ? `<button class="btn-withdraw btn-view-photo" data-id="${it.id}" type="button">📸 Фото</button>` : ''}
          </div>
        </div>
      </div>
    `;
  }
}

export const transfersView = new TransfersView();
