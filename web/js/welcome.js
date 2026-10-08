/**
 * web/js/welcome.js
 * Контроллер приветственного экрана (Welcome Digest) для участников Mini-App.
 */

import { store } from './store.js';
import { tgBridge } from './tg.js';
import { UIRenderer, escapeHtml } from './ui.js';

class WelcomeDigest {
  constructor() {
    this.overlay = null;
    this.container = null;
    this.isShown = false;
    this.init();
  }

  init() {
    this.overlay = document.getElementById('welcome-digest-modal');
    if (!this.overlay) return;
    this.container = this.overlay.querySelector('.welcome-container');

    // Кнопки закрытия
    const closeTop = this.overlay.querySelector('.welcome-btn-close-top');
    const btnEnter = this.overlay.querySelector('#welcome-btn-enter');

    closeTop?.addEventListener('click', () => this.hide());
    btnEnter?.addEventListener('click', () => this.hide());

    // Клик по подложке (вне контейнера) закрывает модалку
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.hide();
    });

    // Обработчики клика по новостным карточкам
    this.overlay.querySelectorAll('.welcome-feed-card').forEach(card => {
      card.addEventListener('click', () => {
        const title = card.dataset.title || card.querySelector('.welcome-feed-title')?.textContent || '';
        const text = card.dataset.text || '';
        if (title && text) this.showDetail(title, text);
      });
    });

    // Модалка подробностей новости
    const detailModal = document.getElementById('welcome-detail-modal');
    detailModal?.querySelector('.welcome-btn-close-detail')?.addEventListener('click', () => {
      detailModal.classList.remove('active');
    });
    detailModal?.addEventListener('click', (e) => {
      if (e.target === detailModal) detailModal.classList.remove('active');
    });
  }

  /**
   * Отобразить приветственный экран при входе в Mini-App.
   */
  show(userData = null) {
    if (!this.overlay) this.init();
    if (!this.overlay) return;

    this.updateDynamicData(userData);

    this.overlay.style.display = 'flex';
    // Небольшая задержка для запуска CSS transitions
    requestAnimationFrame(() => {
      this.overlay.classList.add('active');
      this.container?.classList.remove('closing');
      this.isShown = true;
      tgBridge.hapticImpact('light');
    });
  }

  /**
   * Скрыть приветственный экран.
   */
  hide() {
    if (!this.overlay || !this.isShown) return;
    this.isShown = false;
    tgBridge.hapticImpact('medium');

    this.container?.classList.add('closing');
    this.overlay.classList.remove('active');

    setTimeout(() => {
      this.overlay.style.display = 'none';
      this.container?.classList.remove('closing');
    }, 280);
  }

  /**
   * Показать модальное окно с полным текстом новости.
   */
  showDetail(title, text) {
    const modal = document.getElementById('welcome-detail-modal');
    if (!modal) return;
    tgBridge.hapticImpact('light');

    const titleEl = modal.querySelector('.welcome-detail-title');
    const textEl = modal.querySelector('.welcome-detail-text');

    if (titleEl) titleEl.textContent = title;
    if (textEl) textEl.textContent = text;

    modal.classList.add('active');
  }

  /**
   * Заполнить динамические данные (пользователь, баланс, матч дня) из store/bootstrap.
   */
  updateDynamicData(userData) {
    const user = userData || store.state.user;
    if (!user) return;

    // Имя и клуб
    const nameEl = document.getElementById('welcome-user-name');
    if (nameEl) {
      const club = store.state.myClub?.overview?.team_name || '';
      nameEl.textContent = `${user.first_name || 'Участник'} ${club ? `(${club})` : ''}`;
    }

    // Баланс
    const balEl = document.getElementById('welcome-user-balance');
    if (balEl && typeof user.balance === 'number') {
      balEl.textContent = `${UIRenderer.formatNumber(user.balance)} 🪙`;
    }

    // Центральный матч (если есть в hotMatches)
    const hot = store.state.hotMatches?.[0];
    if (hot) {
      const t1 = document.getElementById('welcome-match-t1');
      const t2 = document.getElementById('welcome-match-t2');
      const p1 = document.getElementById('welcome-odd-p1');
      const px = document.getElementById('welcome-odd-px');
      const p2 = document.getElementById('welcome-odd-p2');

      if (t1) t1.textContent = hot.team1_name || 'Команда 1';
      if (t2) t2.textContent = hot.team2_name || 'Команда 2';
      if (p1) p1.textContent = hot.odds?.p1 || '—';
      if (px) px.textContent = hot.odds?.draw || '—';
      if (p2) p2.textContent = hot.odds?.p2 || '—';
    }
  }
}

export const welcomeDigest = new WelcomeDigest();
