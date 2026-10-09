/**
 * web/js/irl.js
 * Лобби в режиме «🌍 IRL»: ставки на реальные футбольные матчи дня.
 *
 * Как и «Долгосрочные», раздел живёт отдельно от реактивного store: своя доска дня,
 * свои «Мои ставки», панель ставки раскрывается прямо под карточкой матча. Из store
 * берётся только баланс. Всё, что приходит в `betting_open`, — подсказка интерфейсу:
 * ставку заново проверяет `database.place_irl_bet`. Расчёт — по счёту основного
 * времени (90 минут), об этом напоминает `note` из ответа.
 */

import { api } from './api.js';
import { store } from './store.js';
import { tgBridge } from './tg.js';
import { escapeHtml } from './ui.js';

const OUTCOMES = [
  { key: 'home', label: '1' },
  { key: 'draw', label: 'X' },
  { key: 'away', label: '2' },
];

const BET_STATUS = {
  pending: ['В игре', 'pending'],
  won: ['Выигрыш', 'won'],
  lost: ['Проигрыш', 'lost'],
  refunded: ['Возврат', 'refunded'],
};

const QUICK_STAKES = [100, 250, 500, 1000];
const DEFAULT_MAX_BET = 1000;

const fmtOdd = (o) => Number(o || 0).toFixed(2);
const coins = (n) => `${Math.round(Number(n) || 0).toLocaleString('ru-RU')} 🪙`;
const kickoff = (t) => {
  const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(t || ''));
  return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]} МСК` : '';
};
const hasOdds = (m) => ['home', 'draw', 'away'].every(k => Number(m.odds?.[k]) > 1);
const outcomeName = (match, key) => {
  if (key === 'home') return match.home;
  if (key === 'away') return match.away;
  return 'Ничья';
};

class IrlView {
  constructor() {
    this.container = null;
    this.board = null;
    this.loading = false;
    this.error = null;
    this.tab = 'today'; // today | my
    this.my = null;
    this.myLoading = false;
    this.picks = [];    // [{matchId, outcome, odd}]
    this.stake = '';
    this.busy = false;
    this.notice = '';
    this.slipError = '';
    this._bound = false;
  }

  get pick() {
    return this.picks.length === 1 ? this.picks[0] : null;
  }

  mount() {
    if (!this.container) {
      this.container = document.getElementById('irl-view-container');
    }
    if (!this.container || this._bound) return;
    this._bound = true;
    this.container.addEventListener('click', (e) => this.onClick(e));
    this.container.addEventListener('input', (e) => {
      if (!e.target.matches('.irl-stake-input')) return;
      this.stake = e.target.value.replace(/\D/g, '');
      this.updateTotals();
    });
  }

  async open() {
    this.mount();
    this.render();
    await this.loadBoard();
  }

  get maxBet() {
    return Number(this.board?.max_bet) || DEFAULT_MAX_BET;
  }

  totalOdd() {
    if (!this.picks.length) return 0;
    if (this.picks.length === 1) return Number(this.picks[0].odd || 0);
    let raw = 1.0;
    for (const p of this.picks) {
      raw *= Number(p.odd || 1);
    }
    return Math.round(raw * 100) / 100;
  }

  // ─── Данные ─────────────────────────────────────────────────────────────

  async loadBoard() {
    this.loading = true;
    this.error = null;
    this.render();
    try {
      this.board = await api.getIrlToday();
    } catch (e) {
      this.error = e.message || 'Не удалось загрузить матчи';
    } finally {
      this.loading = false;
    }
    // Отсеять матчи, где закрылся приём ставок
    const openIds = new Set((this.board?.matches || []).filter(m => m.betting_open).map(m => m.id));
    this.picks = this.picks.filter(p => openIds.has(p.matchId));
    this.render();
  }

  async loadMy() {
    this.myLoading = true;
    this.render();
    try {
      this.my = await api.getMyIrlBets();
    } catch (e) {
      this.my = { error: e.message || 'Не удалось загрузить ставки', bets: [] };
    } finally {
      this.myLoading = false;
    }
    if (this.tab === 'my') this.render();
  }

  // ─── Отрисовка ─────────────────────────────────────────────────────────

  render() {
    if (!this.container) return;
    const openBets = (this.board?.matches || []).filter(m => m.my_bet?.status === 'pending').length;
    const tabs = `
      <div class="ob-groups scroll-row">
        <button class="ob-group-btn ${this.tab === 'today' ? 'active' : ''}" data-irl-tab="today"><span>⚽</span>Матчи дня</button>
        <button class="ob-group-btn ${this.tab === 'my' ? 'active' : ''}" data-irl-tab="my">
          <span>🎫</span>Мои ставки${openBets ? `<span class="ob-count">${openBets}</span>` : ''}
        </button>
      </div>`;

    let body;
    if (this.tab === 'my') body = this.renderMy();
    else if (this.loading && !this.board) body = '<div class="irl-state">Загружаем матчи…</div>';
    else if (this.error && !this.board) {
      body = `<div class="irl-state">${escapeHtml(this.error)}<br><button class="ob-chip irl-retry" data-irl-retry="1">Повторить</button></div>`;
    } else body = this.renderToday();

    this.container.innerHTML = tabs + body;
  }

  renderToday() {
    const matches = this.board?.matches || [];
    const note = this.board?.note ? `<div class="ob-banner">${escapeHtml(this.board.note)}</div>` : '';
    const limits = `<div class="ob-banner lock">Одиночные ставки и экспрессы (2–5 событий). Максимум ${coins(this.maxBet)}. Только исход 1X2.</div>`;
    if (!matches.length) {
      return `${note}<div class="irl-state">Сегодня подходящих матчей нет — загляните завтра.</div>`;
    }
    const matchesHtml = matches.map(m => this.renderMatch(m)).join('');
    const expressSlipHtml = this.picks.length >= 2 ? this.renderExpressSlip() : '';
    return `${note}${limits}${matchesHtml}${expressSlipHtml}`;
  }

  renderMatch(m) {
    const bet = m.my_bet;
    const currentPick = this.picks.find(p => p.matchId === m.id);
    const canBet = m.betting_open && !bet && hasOdds(m);
    const isSingleSelected = this.picks.length === 1 && currentPick;

    let score = '';
    if (m.status === 'settled' && m.home_goals != null && m.away_goals != null) {
      score = `<div class="irl-score">${Number(m.home_goals)} : ${Number(m.away_goals)}</div>`;
    } else if (m.status === 'closed' && (m.home_goals != null || m.away_goals != null)) {
      score = `<div class="irl-score irl-live-score">${Number(m.home_goals || 0)} : ${Number(m.away_goals || 0)}</div>`;
    }

    let state = '';
    if (m.status === 'void') {
      state = `<div class="ob-banner lock">Матч не состоялся — ставки возвращены${m.void_reason ? ` (${escapeHtml(m.void_reason)})` : ''}</div>`;
    } else if (m.status === 'settled') {
      state = '<div class="irl-final">Матч сыгран</div>';
    } else if (m.status === 'closed' && (m.home_goals != null || m.away_goals != null)) {
      state = '<div class="irl-live-badge"><span class="irl-live-dot"></span> LIVE</div>';
    } else if (!m.betting_open) {
      state = '<div class="ob-banner lock">⛔ Приём ставок закрыт</div>';
    }

    const odds = hasOdds(m) ? `
      <div class="irl-odds">
        ${OUTCOMES.map(o => {
          const mine = bet && bet.outcome === o.key;
          const sel = currentPick && currentPick.outcome === o.key;
          const cls = ['irl-odd', canBet ? '' : 'locked', sel ? 'selected' : '', mine ? 'mine' : '',
            m.result === o.key ? 'winner' : ''].filter(Boolean).join(' ');
          const attrs = canBet ? `data-irl-pick="${m.id}" data-outcome="${o.key}"` : '';
          return `<div class="${cls}" ${attrs}>
            <span class="odd-label">${o.label}</span>
            <span class="odd-val">${fmtOdd(m.odds[o.key])}</span>
          </div>`;
        }).join('')}
      </div>` : '<div class="ob-banner lock">Коэффициенты пока не загружены</div>';

    return `
      <div class="irl-card" data-irl-match="${m.id}">
        <div class="irl-league">${escapeHtml(m.league_name || 'Футбол')} · ${escapeHtml(kickoff(m.kickoff_at))}</div>
        <div class="irl-teams">
          <span class="irl-team">${escapeHtml(m.home)}</span>
          ${score || '<span class="irl-vs">VS</span>'}
          <span class="irl-team right">${escapeHtml(m.away)}</span>
        </div>
        ${state}
        ${odds}
        ${bet ? this.renderMyBetLine(bet, m) : ''}
        ${isSingleSelected && canBet ? this.renderSlip(m) : ''}
      </div>`;
  }

  renderMyBetLine(bet, m) {
    const [label, cls] = BET_STATUS[bet.status] || [bet.status, 'pending'];
    const payout = bet.status === 'won' || bet.status === 'refunded'
      ? ` · ${coins(bet.actual_payout)}` : ` · выигрыш ${coins(bet.potential_win)}`;
    const expBadge = bet.is_express ? '⚡ В экспрессе · ' : '';
    return `
      <div class="irl-mybet ${cls}">
        <b>Моя ставка:</b> ${expBadge}${escapeHtml(outcomeName(m, bet.outcome))} @ ${fmtOdd(bet.odd)} · ${coins(bet.amount)}
        <span class="irl-mybet-status">${escapeHtml(label)}${bet.status === 'lost' ? '' : escapeHtml(payout)}</span>
      </div>`;
  }

  renderSlip(m) {
    const p = this.pick;
    const max = this.maxBet;
    if (!p) return '';
    return `
      <div class="irl-slip">
        <div class="irl-slip-pick">
          <span>Ординар: ${escapeHtml(outcomeName(m, p.outcome))}</span>
          <span class="irl-slip-odd">${fmtOdd(p.odd)}</span>
        </div>
        <div class="ob-sheet-notice irl-slip-notice" ${this.notice ? '' : 'hidden'}>${escapeHtml(this.notice || '')}</div>
        <label class="ob-stake">Сумма ставки (до ${coins(max)})
          <input class="ob-stake-input irl-stake-input" type="text" inputmode="numeric" autocomplete="off"
                 placeholder="Например, 100" value="${escapeHtml(this.stake)}">
        </label>
        <div class="ob-quick">
          ${QUICK_STAKES.filter(v => v <= max).map(v => `<button class="ob-chip" data-irl-quick="${v}">${v}</button>`).join('')}
        </div>
        <div class="ob-sheet-totals">
          <span>Возможный выигрыш: <b class="ob-sheet-win irl-win">${coins(this.potentialWin())}</b></span>
        </div>
        <div class="irl-slip-error" ${this.slipError ? '' : 'hidden'}>${escapeHtml(this.slipError || '')}</div>
        <div class="irl-slip-actions">
          <button class="ob-chip" data-irl-cancel="1">Отмена</button>
          <button class="irl-confirm" data-irl-confirm="1" ${this.busy ? 'disabled' : ''}>${this.busy ? 'Отправляем…' : 'Поставить ординар'}</button>
        </div>
      </div>`;
  }

  renderExpressSlip() {
    const max = this.maxBet;
    return `
      <div class="irl-card irl-express-card" style="border: 2px solid var(--accent-gold); box-shadow: 0 4px 16px rgba(0,0,0,0.25); margin-top: 16px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
          <div style="font-family:'Outfit', sans-serif; font-size:1.1rem; font-weight:800; color:var(--text-primary); display:flex; align-items:center; gap:6px;">
            <span>⚡</span> Экспресс (${this.picks.length} события)
          </div>
          <div style="font-family:'Outfit', sans-serif; font-size:1.3rem; font-weight:800; color:var(--accent-gold);">
            @ ${fmtOdd(this.totalOdd())}
          </div>
        </div>
        <div class="irl-express-legs" style="display:flex; flex-direction:column; gap:8px; margin-bottom:12px;">
          ${this.picks.map(p => {
            const m = (this.board?.matches || []).find(match => match.id === p.matchId);
            const matchTitle = m ? `${m.home} — ${m.away}` : `Матч #${p.matchId}`;
            const pickTitle = m ? outcomeName(m, p.outcome) : p.outcome;
            return `
              <div style="display:flex; justify-content:space-between; align-items:center; background:rgba(255,255,255,0.04); padding:8px 12px; border-radius:8px; font-size:0.85rem;">
                <div style="flex:1; min-width:0; padding-right:8px;">
                  <div style="color:var(--text-muted); font-size:0.75rem;">${escapeHtml(matchTitle)}</div>
                  <div style="font-weight:700; color:var(--text-primary);">${escapeHtml(pickTitle)} <span style="color:var(--accent-gold);">@ ${fmtOdd(p.odd)}</span></div>
                </div>
                <button class="ob-chip" data-irl-remove-leg="${p.matchId}" style="padding:4px 8px; font-size:0.75rem; border-radius:6px; cursor:pointer;" title="Убрать из экспресса">✕</button>
              </div>
            `;
          }).join('')}
        </div>
        <div class="ob-sheet-notice irl-slip-notice" ${this.notice ? '' : 'hidden'}>${escapeHtml(this.notice || '')}</div>
        <label class="ob-stake">Сумма ставки (до ${coins(max)})
          <input class="ob-stake-input irl-stake-input" type="text" inputmode="numeric" autocomplete="off"
                 placeholder="Например, 100" value="${escapeHtml(this.stake)}">
        </label>
        <div class="ob-quick">
          ${QUICK_STAKES.filter(v => v <= max).map(v => `<button class="ob-chip" data-irl-quick="${v}">${v}</button>`).join('')}
        </div>
        <div class="ob-sheet-totals">
          <span>Возможный выигрыш: <b class="ob-sheet-win irl-win">${coins(this.potentialWin())}</b></span>
        </div>
        <div class="irl-slip-error" ${this.slipError ? '' : 'hidden'}>${escapeHtml(this.slipError || '')}</div>
        <div class="irl-slip-actions">
          <button class="ob-chip" data-irl-clear-express="1">Сбросить</button>
          <button class="irl-confirm" data-irl-confirm-express="1" ${this.busy ? 'disabled' : ''}>${this.busy ? 'Отправляем…' : 'Поставить экспресс'}</button>
        </div>
      </div>`;
  }

  renderMy() {
    if (!this.my && !this.myLoading) {
      queueMicrotask(() => this.loadMy());
    }
    if (this.myLoading && !this.my) return '<div class="irl-state">Загружаем ставки…</div>';
    if (this.my?.error) return `<div class="irl-state">${escapeHtml(this.my.error)}</div>`;
    const bets = this.my?.bets || [];
    if (!bets.length) return '<div class="irl-state">Ставок на реальные матчи пока нет.</div>';

    return bets.map(b => {
      const [label, cls] = BET_STATUS[b.status] || [b.status, 'pending'];
      const result = b.status === 'won' || b.status === 'refunded'
        ? `${escapeHtml(label)} · ${coins(b.actual_payout)}`
        : b.status === 'lost' ? escapeHtml(label) : `${escapeHtml(label)} · выигрыш ${coins(b.potential_win)}`;

      if (b.bet_type === 'express') {
        const items = b.items || [];
        return `
          <div class="irl-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <div class="irl-league">⚡ Экспресс (${items.length} соб.) · ${escapeHtml(kickoff(b.created_at))}</div>
              <span class="irl-mybet-status" style="font-size:0.75rem;">${escapeHtml(label)}</span>
            </div>
            <div class="irl-express-my-legs" style="display:flex; flex-direction:column; gap:6px; margin-bottom:10px;">
              ${items.map(it => {
                const [itLabel, itCls] = BET_STATUS[it.status] || [it.status, 'pending'];
                const score = it.match_status === 'settled' && it.home_goals != null && it.away_goals != null
                  ? ` (${it.home_goals}:${it.away_goals})` : '';
                return `
                  <div style="display:flex; justify-content:space-between; align-items:center; background:rgba(255,255,255,0.03); padding:6px 10px; border-radius:6px; font-size:0.8rem;">
                    <div style="flex:1; min-width:0; padding-right:8px;">
                      <div style="color:var(--text-muted); font-size:0.72rem;">${escapeHtml(it.home)} — ${escapeHtml(it.away)}${score}</div>
                      <div style="font-weight:700; color:var(--text-primary);">${escapeHtml(outcomeName(it, it.outcome))} <span style="color:var(--accent-gold);">@ ${fmtOdd(it.odd)}</span></div>
                    </div>
                    <span class="irl-mybet-status ${itCls}" style="font-size:0.75rem; font-weight:700;">${escapeHtml(itLabel)}</span>
                  </div>
                `;
              }).join('')}
            </div>
            <div class="irl-mybet ${cls}">
              <b>Экспресс:</b> ${coins(b.amount)} @ ${fmtOdd(b.odd)}
              <span class="irl-mybet-status">${result}</span>
            </div>
          </div>`;
      }

      const score = b.match_status === 'settled' && b.home_goals != null && b.away_goals != null
        ? ` · ${Number(b.home_goals)}:${Number(b.away_goals)}` : '';
      return `
        <div class="irl-card">
          <div class="irl-league">${escapeHtml(b.league_name || 'Футбол')} · ${escapeHtml(kickoff(b.kickoff_at))}</div>
          <div class="irl-teams">
            <span class="irl-team">${escapeHtml(b.home)}</span>
            <span class="irl-vs">VS</span>
            <span class="irl-team right">${escapeHtml(b.away)}</span>
          </div>
          <div class="irl-mybet ${cls}">
            ${escapeHtml(outcomeName({ home: b.home, away: b.away }, b.outcome))} @ ${fmtOdd(b.odd)} · ${coins(b.amount)}${escapeHtml(score)}
            <span class="irl-mybet-status">${result}</span>
          </div>
        </div>`;
    }).join('');
  }

  // ─── Ставка ─────────────────────────────────────────────────────────────

  stakeValue() {
    const v = parseInt(this.stake, 10);
    return Number.isFinite(v) ? v : 0;
  }

  potentialWin() {
    if (!this.picks.length) return 0;
    return Math.floor(this.stakeValue() * this.totalOdd());
  }

  updateTotals() {
    const el = this.container?.querySelector('.irl-win');
    if (el) el.textContent = coins(this.potentialWin());
  }

  onClick(e) {
    const tabBtn = e.target.closest('[data-irl-tab]');
    if (tabBtn) {
      tgBridge.hapticImpact('light');
      this.tab = tabBtn.dataset.irlTab;
      if (this.tab === 'my') this.my = null;
      this.render();
      return;
    }
    if (e.target.closest('[data-irl-retry]')) {
      this.loadBoard();
      return;
    }
    const pickBtn = e.target.closest('[data-irl-pick]');
    if (pickBtn) {
      const matchId = parseInt(pickBtn.dataset.irlPick, 10);
      const outcome = pickBtn.dataset.outcome;
      const match = (this.board?.matches || []).find(m => m.id === matchId);
      if (!match) return;
      tgBridge.hapticImpact('light');

      const existingIdx = this.picks.findIndex(p => p.matchId === matchId);
      if (existingIdx >= 0) {
        if (this.picks[existingIdx].outcome === outcome) {
          // Повторный тап по тому же исходу снимает выбор
          this.picks.splice(existingIdx, 1);
        } else {
          // Тап по другому исходу того же матча меняет исход
          this.picks[existingIdx] = { matchId, outcome, odd: Number(match.odds[outcome]) };
        }
      } else {
        if (this.picks.length >= 5) {
          this.toast('В экспрессе может быть максимум 5 событий');
          return;
        }
        this.picks.push({ matchId, outcome, odd: Number(match.odds[outcome]) });
      }
      this.notice = '';
      this.slipError = '';
      this.render();
      return;
    }
    const removeLegBtn = e.target.closest('[data-irl-remove-leg]');
    if (removeLegBtn) {
      const matchId = parseInt(removeLegBtn.dataset.irlRemoveLeg, 10);
      this.picks = this.picks.filter(p => p.matchId !== matchId);
      this.render();
      return;
    }
    const quick = e.target.closest('[data-irl-quick]');
    if (quick) {
      this.stake = String(Math.min(parseInt(quick.dataset.irlQuick, 10), this.maxBet));
      const input = this.container.querySelector('.irl-stake-input');
      if (input) input.value = this.stake;
      this.updateTotals();
      return;
    }
    if (e.target.closest('[data-irl-cancel]') || e.target.closest('[data-irl-clear-express]')) {
      this.picks = [];
      this.stake = '';
      this.notice = '';
      this.slipError = '';
      this.render();
      return;
    }
    if (e.target.closest('[data-irl-confirm]')) {
      this.submitSingle();
      return;
    }
    if (e.target.closest('[data-irl-confirm-express]')) {
      this.submitExpress();
      return;
    }
  }

  setSlipMessage({ notice, error }) {
    if (notice !== undefined) this.notice = notice;
    if (error !== undefined) this.slipError = error;
    const n = this.container.querySelector('.irl-slip-notice');
    if (n) { n.hidden = !this.notice; n.textContent = this.notice; }
    const er = this.container.querySelector('.irl-slip-error');
    if (er) { er.hidden = !this.slipError; er.textContent = this.slipError; }
    this.updateTotals();
  }

  async submitSingle() {
    const p = this.pick;
    if (!p || this.busy) return;
    const amount = this.stakeValue();
    if (amount < 1) {
      this.setSlipMessage({ error: 'Введите сумму ставки.' });
      return;
    }
    if (amount > this.maxBet) {
      this.setSlipMessage({ error: `Максимум на матч — ${this.maxBet} 🪙.` });
      return;
    }
    const button = this.container.querySelector('[data-irl-confirm]');
    this.busy = true;
    this.setSlipMessage({ error: '' });
    if (button) { button.disabled = true; button.textContent = 'Отправляем…'; }
    try {
      const res = await api.placeIrlBet({ match_id: p.matchId, outcome: p.outcome, amount, odd: p.odd });
      if (store.state.user && res.balance != null) store.setUser({ ...store.state.user, balance: res.balance });
      tgBridge.hapticNotification('success');
      this.toast(`Ставка #${res.bet_id} принята · выигрыш ${coins(res.potential_win)}`);
      this.picks = [];
      this.stake = '';
      this.my = null;
      await this.loadBoard();
    } catch (err) {
      const code = err.data?.error || err.code;
      tgBridge.hapticNotification(code === 'ODDS_CHANGED' ? 'warning' : 'error');
      if (code === 'ODDS_CHANGED' && err.data?.new_odd) {
        const old = p.odd;
        p.odd = Number(err.data.new_odd);
        this.setSlipMessage({ notice: `Коэффициент изменился: ${fmtOdd(old)} → ${fmtOdd(p.odd)}. Подтвердите ставку ещё раз.` });
        this.refreshOddsQuietly();
      } else {
        this.setSlipMessage({ error: err.message || 'Ставка не принята.' });
        if (['IRL_ALREADY_BET', 'IRL_BETTING_CLOSED', 'MARKET_SUSPENDED'].includes(code)) {
          this.picks = [];
          await this.loadBoard();
        }
      }
    } finally {
      this.busy = false;
      const btn = this.container.querySelector('[data-irl-confirm]');
      if (btn) { btn.disabled = false; btn.textContent = 'Поставить ординар'; }
    }
  }

  async submitExpress() {
    if (this.picks.length < 2 || this.busy) return;
    const amount = this.stakeValue();
    if (amount < 1) {
      this.setSlipMessage({ error: 'Введите сумму ставки.' });
      return;
    }
    if (amount > this.maxBet) {
      this.setSlipMessage({ error: `Максимум на экспресс — ${this.maxBet} 🪙.` });
      return;
    }
    const button = this.container.querySelector('[data-irl-confirm-express]');
    this.busy = true;
    this.setSlipMessage({ error: '' });
    if (button) { button.disabled = true; button.textContent = 'Отправляем…'; }
    try {
      const items = this.picks.map(p => ({ match_id: p.matchId, outcome: p.outcome, odd: p.odd }));
      const res = await api.placeIrlBet({ items, amount });
      if (store.state.user && res.balance != null) store.setUser({ ...store.state.user, balance: res.balance });
      tgBridge.hapticNotification('success');
      this.toast(`⚡ Экспресс #${res.bet_id} принят · выигрыш ${coins(res.potential_win)}`);
      this.picks = [];
      this.stake = '';
      this.my = null;
      await this.loadBoard();
    } catch (err) {
      const code = err.data?.error || err.code;
      tgBridge.hapticNotification(code === 'ODDS_CHANGED' ? 'warning' : 'error');
      this.setSlipMessage({ error: err.message || 'Экспресс не принят.' });
      if (['IRL_ALREADY_BET', 'IRL_BETTING_CLOSED', 'MARKET_SUSPENDED'].includes(code)) {
        await this.loadBoard();
      }
    } finally {
      this.busy = false;
      const btn = this.container.querySelector('[data-irl-confirm-express]');
      if (btn) { btn.disabled = false; btn.textContent = 'Поставить экспресс'; }
    }
  }

  async refreshOddsQuietly() {
    try {
      this.board = await api.getIrlToday();
      this.render();
    } catch (_) { }
  }

  toast(message) {
    let el = document.getElementById('irl-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'irl-toast';
      el.className = 'adm-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }
}

export const irlView = new IrlView();
