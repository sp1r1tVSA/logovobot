/**
 * web/js/transfers.js
 * Интерфейс трансферного окна («ТО») в Telegram Mini App.
 *
 * 4 вкладки:
 *  1. Статус   — состояние окна, бюджет и слоты клуба, входящие предложения, мои заявки
 *  2. Заявка   — форма подачи сделки / доплаты за спешл / продажи в урну (с фото)
 *  3. Урна     — список доступных игроков для выкупа
 *  4. История  — лента одобренных трансферов лиги (с просмотром фото)
 */

import { api } from './api.js';
import { tgBridge } from './tg.js';
import { escapeHtml } from './ui.js';

class TransfersView {
  constructor() {
    this.root = null;
    this.activeTab = 'status';
    this.requestKind = 'deal';
    this.dealRole = 'buy';
    this.statusData = null;
    this.urnData = null;
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
          <button class="transfers-tab-btn ${this.activeTab === 'urn' ? 'active' : ''}" data-tab="urn" type="button">🗑 Урна</button>
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
    } else if (this.activeTab === 'urn') {
      await this.loadUrn(container, force);
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
    const incomingDeals = (requests || []).filter(r => r.can_confirm);
    let incomingHtml = '';
    if (incomingDeals.length > 0) {
      incomingHtml = `
        <div class="incoming-deals-box">
          <div class="incoming-deals-title">⚡️ Ждут вашего подтверждения (${incomingDeals.length})</div>
          ${incomingDeals.map(r => this.renderRequestItem(r)).join('')}
        </div>`;
    }

    // Мои заявки
    const ownRequests = (requests || []).filter(r => !r.can_confirm);
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
            ${kindMap[r.kind] || r.kind} #${r.id}: <b>${escapeHtml(r.player_name)}</b>
            ${r.ovr ? `<span class="req-ovr-tag">OVR ${r.ovr}</span>` : ''}
          </span>
          <span class="window-status-badge ${st.cls}">${st.label}</span>
        </div>

        <div class="req-route">
          ${r.from_club ? `Откуда: <b>${escapeHtml(r.from_club)}</b> ` : ''}
          ${r.to_club ? `→ Куда: <b>${escapeHtml(r.to_club)}</b>` : ''}
        </div>

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
          <div class="form-group" style="margin-top: 14px;">
            <label class="form-label">Скриншот / фото (необязательно)</label>
            <div class="photo-upload-zone" id="photo-dropzone">
              <span id="photo-upload-prompt">📷 Нажмите, чтобы прикрепить фото</span>
              <input type="file" id="transfer-photo-input" accept="image/jpeg,image/png,image/webp">
              <div id="photo-preview-container" style="display: none;"></div>
            </div>
          </div>

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

  renderFormFields() {
    const fieldsContainer = document.getElementById('transfer-form-fields');
    if (!fieldsContainer) return;

    if (this.requestKind === 'deal') {
      fieldsContainer.innerHTML = `
        <div class="role-toggle-row">
          <button class="role-toggle-btn ${this.dealRole === 'buy' ? 'active buy' : ''}" data-role="buy" type="button">🟢 Я покупаю игрока</button>
          <button class="role-toggle-btn ${this.dealRole === 'sell' ? 'active sell' : ''}" data-role="sell" type="button">🔴 Я продаю игрока</button>
        </div>

        <div class="form-group">
          <label class="form-label" for="deal-other-club">${this.dealRole === 'buy' ? 'У какого клуба покупаете' : 'Какому клубу продаёте'}</label>
          <input type="text" id="deal-other-club" class="form-input" placeholder="Название клуба соперника" required>
        </div>

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
        </div>
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

    // Role toggle bindings
    fieldsContainer.querySelectorAll('.role-toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.dealRole = btn.dataset.role;
        this.renderFormFields();
      });
    });
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

        if (this.requestKind === 'deal') {
          formData.append('role', this.dealRole);
          formData.append('other_club', document.getElementById('deal-other-club').value);
          formData.append('player', document.getElementById('deal-player').value);
          formData.append('ovr', document.getElementById('deal-ovr').value);
          formData.append('price', document.getElementById('deal-price').value);
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

  // ─── 3. Вкладка «Урна» ─────────────────────────────────────────────────────

  async loadUrn(container, force = false) {
    if (!this.urnData || force) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">⏳</div>
          <div>Загрузка урны...</div>
        </div>`;
      try {
        const res = await api.getTransferUrn();
        if (res.status === 'ok') {
          this.urnData = res.data;
        } else {
          container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(res.message || 'Ошибка')}</div>`;
          return;
        }
      } catch (err) {
        container.innerHTML = `<div class="transfers-empty">⚠️ ${escapeHtml(err.message || 'Ошибка сети')}</div>`;
        return;
      }
    }

    const { items, can_buy } = this.urnData;
    if (!items || items.length === 0) {
      container.innerHTML = `
        <div class="transfers-empty">
          <div class="transfers-empty-icon">🗑</div>
          <div style="font-weight: 700; color: #fff; margin-bottom: 4px;">Урна пуста</div>
          <div style="font-size: 0.8rem;">В этом окне пока нет карточек на выкуп.</div>
        </div>`;
      return;
    }

    container.innerHTML = `
      <div style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 12px;">
        Карточки игроков, проданные в урну. Любой тренер может выкупить игрока по полной цене (TM + Спешл). Тратит 1 слот покупки.
      </div>
      ${items.map(it => `
        <div class="urn-item-card" data-id="${it.id}">
          <div class="urn-item-info">
            <div class="urn-item-name">${escapeHtml(it.player_name)} ${it.ovr ? `<span class="req-ovr-tag">OVR ${it.ovr}</span>` : ''}</div>
            <div class="urn-item-club">Из клуба: <b>${escapeHtml(it.from_club || '—')}</b></div>
            <div class="urn-item-price">Цена выкупа: <b>${it.buy_price}</b></div>
          </div>
          <div>
            ${can_buy ? `<button class="btn-buyout" data-id="${it.id}" type="button">Выкупить</button>` : '<span style="font-size: 0.72rem; color: var(--text-muted);">Недоступно</span>'}
          </div>
        </div>
      `).join('')}
    `;

    container.querySelectorAll('.btn-buyout').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Выкупить этого игрока из урны?')) return;
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = '...';
        try {
          await api.createTransferUrnBuy(id);
          tgBridge.hapticImpact('heavy');
          alert('Заявка на выкуп подана!');
          this.urnData = null;
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
  }

  // ─── 4. Вкладка «История» ──────────────────────────────────────────────────

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

    return `
      <div class="req-item">
        <div class="req-item-head">
          <span class="req-item-title">
            ${kindLabel}: <b>${escapeHtml(it.player_name)}</b>
            ${it.ovr ? `<span class="req-ovr-tag">OVR ${it.ovr}</span>` : ''}
          </span>
          <span class="window-status-badge ${st.cls}">${st.label}</span>
        </div>
        <div class="req-route">
          ${it.from_club ? `Откуда: <b>${escapeHtml(it.from_club)}</b> ` : ''}
          ${it.to_club ? `→ Куда: <b>${escapeHtml(it.to_club)}</b>` : ''}
        </div>
        ${mine && it.decided_reason ? `
          <div style="font-size: 0.74rem; color: var(--color-danger); margin: 4px 0;">
            Причина: ${escapeHtml(it.decided_reason)}
          </div>` : ''}
        <div class="req-footer">
          <div class="req-price">${it.price}</div>
          ${it.has_photo ? `<button class="btn-withdraw btn-view-photo" data-id="${it.id}" type="button">📸 Фото</button>` : ''}
        </div>
      </div>
    `;
  }
}

export const transfersView = new TransfersView();
