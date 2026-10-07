/**
 * web/js/tg.js
 * Telegram Mini App WebApp API Bridge & Haptic Feedback Controller.
 */

class TelegramBridge {
  constructor() {
    this._tg = null;
    this._ready = false;
    this.init();
  }

  // SDK подключён с defer и обычно уже исполнен к загрузке модулей, но если
  // он запоздал (медленная сеть), берём WebApp при первом обращении, а не
  // навсегда запоминаем null из конструктора.
  get tg() {
    if (!this._tg) {
      this._tg = window.Telegram?.WebApp || null;
      if (this._tg && !this._ready) this.init();
    }
    return this._tg;
  }

  init() {
    const tg = this._tg || window.Telegram?.WebApp || null;
    if (tg && !this._ready) {
      this._tg = tg;
      this._ready = true;
      this.tg.ready();
      this.tg.expand();
      this.syncChrome();
    }
  }

  // Цвет шапки и фона Telegram следует выбранному дизайну (web/js/design.js).
  syncChrome() {
    const color = document.documentElement.getAttribute('data-design') === 'frost' ? '#eaf1ee' : '#080a0e';
    try {
      this._tg?.setHeaderColor(color);
      this._tg?.setBackgroundColor(color);
    } catch (e) {}
  }

  getInitData() {
    if (this.tg && this.tg.initData) {
      return this.tg.initData;
    }
    // Development fallback mock
    return "mock_admin_12345";
  }

  getUser() {
    return this.tg?.initDataUnsafe?.user || null;
  }

  hapticImpact(style = 'light') {
    try {
      this.tg?.HapticFeedback?.impactOccurred(style);
    } catch (e) {}
  }

  hapticNotification(type = 'success') {
    try {
      this.tg?.HapticFeedback?.notificationOccurred(type);
    } catch (e) {}
  }

  showBackButton(onClick) {
    if (this.tg?.BackButton) {
      // Telegram keeps every onClick handler, so drop the previous one first.
      this.hideBackButton();
      this._backHandler = onClick;
      this.tg.BackButton.onClick(onClick);
      this.tg.BackButton.show();
    }
  }

  hideBackButton() {
    if (this.tg?.BackButton) {
      if (this._backHandler) {
        this.tg.BackButton.offClick(this._backHandler);
        this._backHandler = null;
      }
      this.tg.BackButton.hide();
    }
  }

  // MainButton дублирует CTA купона внизу экрана Telegram. Как и у BackButton,
  // Telegram копит все onClick, поэтому прежний обработчик снимаем.
  hasMainButton() {
    return !!(this.tg?.MainButton && this.tg?.initData);
  }

  showMainButton(text, onClick) {
    const mb = this.tg?.MainButton;
    if (!mb) return;
    if (this._mainHandler !== onClick) {
      if (this._mainHandler) mb.offClick(this._mainHandler);
      this._mainHandler = onClick;
      mb.onClick(onClick);
    }
    if (text) mb.setText(text);
    mb.show();
  }

  updateMainButton({ text, enabled = true, loading = false } = {}) {
    const mb = this.tg?.MainButton;
    if (!mb || !mb.isVisible) return;
    try {
      if (text && mb.text !== text) mb.setText(text);
      if (enabled) mb.enable(); else mb.disable();
      if (loading) mb.showProgress(false); else mb.hideProgress();
    } catch (e) {}
  }

  hideMainButton() {
    const mb = this.tg?.MainButton;
    if (!mb) return;
    if (this._mainHandler) {
      mb.offClick(this._mainHandler);
      this._mainHandler = null;
    }
    try { mb.hideProgress(); } catch (e) {}
    mb.hide();
  }

  // CloudStorage хранит настройки между устройствами; вне Telegram (или на
  // старом клиенте) — localStorage, который тоже может бросить исключение.
  cloudGet(key) {
    return new Promise((resolve) => {
      const local = () => {
        try { resolve(localStorage.getItem(`lb:${key}`)); } catch (e) { resolve(null); }
      };
      const cs = this.tg?.CloudStorage;
      if (!cs || !this.tg?.initData) return local();
      try {
        cs.getItem(key, (err, value) => (err ? local() : resolve(value || null)));
      } catch (e) { local(); }
    });
  }

  cloudSet(key, value) {
    const str = value == null ? '' : String(value);
    try { localStorage.setItem(`lb:${key}`, str); } catch (e) {}
    const cs = this.tg?.CloudStorage;
    if (!cs || !this.tg?.initData) return;
    try { cs.setItem(key, str, () => {}); } catch (e) {}
  }

  // `startapp` из ссылки t.me/<bot>/<app>?startapp=match_123
  getStartParam() {
    return this.tg?.initDataUnsafe?.start_param
      || new URLSearchParams(window.location.search).get('tgWebAppStartParam')
      || '';
  }

  // Отправить текст в чат: inline-режим бота, иначе стандартная ссылка «поделиться».
  share(text, url = '') {
    try {
      if (this.tg?.switchInlineQuery && this.tg?.initDataUnsafe?.user) {
        this.tg.switchInlineQuery(text, ['users', 'groups', 'channels']);
        return;
      }
    } catch (e) {}
    const link = `https://t.me/share/url?url=${encodeURIComponent(url || ' ')}&text=${encodeURIComponent(text)}`;
    try {
      if (this.tg?.openTelegramLink) { this.tg.openTelegramLink(link); return; }
    } catch (e) {}
    window.open(link, '_blank', 'noopener');
  }

  showAlert(message, callback = null) {
    try {
      if (this.tg?.showAlert) {
        this.tg.showAlert(String(message), callback || (() => {}));
        return;
      }
    } catch (e) {}
    alert(message);
    if (typeof callback === 'function') callback();
  }

  showConfirm(message, callback = null) {
    try {
      if (this.tg?.showConfirm) {
        this.tg.showConfirm(String(message), callback || (() => {}));
        return;
      }
    } catch (e) {}
    const res = confirm(message);
    if (typeof callback === 'function') callback(res);
  }

  close() {
    try {
      this.tg?.close();
    } catch (e) {}
  }
}

export const tgBridge = new TelegramBridge();
