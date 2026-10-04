/**
 * web/js/design.js
 * Переключатель дизайна: «dark» (прежний) и «frost» («Матовое стекло»).
 * Выбор лежит в localStorage; до отрисовки его восстанавливает inline-скрипт
 * в <head> index.html, здесь — кнопки выбора и цвета шапки Telegram.
 */
import { tgBridge } from './tg.js';

const STORAGE_KEY = 'logovo.design';

export function getDesign() {
  return document.documentElement.getAttribute('data-design') === 'frost' ? 'frost' : 'dark';
}

function markActive() {
  const current = getDesign();
  document.querySelectorAll('[data-design-choice]').forEach(btn => {
    const on = btn.dataset.designChoice === current;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

export function setDesign(design) {
  const root = document.documentElement;
  if (design === 'frost') root.setAttribute('data-design', 'frost');
  else root.removeAttribute('data-design');
  try {
    if (design === 'frost') localStorage.setItem(STORAGE_KEY, 'frost');
    else localStorage.removeItem(STORAGE_KEY);
  } catch (e) {}
  tgBridge.syncChrome();
  markActive();
}

export function initDesign() {
  markActive();
  tgBridge.syncChrome();
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-design-choice]');
    if (!btn) return;
    const next = btn.dataset.designChoice === 'frost' ? 'frost' : 'dark';
    if (next === getDesign()) return;
    setDesign(next);
    tgBridge.hapticImpact('light');
  });
}
