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
      // Apply header color
      try {
        this.tg.setHeaderColor('#080a0e');
        this.tg.setBackgroundColor('#080a0e');
      } catch (e) {}
    }
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
