/**
 * web/js/outrights.js
 * Лобби в режиме «📈 Долгосрочные»: победители дивизионов и кубков, бомбардиры.
 *
 * Раздел живёт отдельно от реактивного store: у него своя доска рынков, история
 * цен для графика и лист ставки. Из store берутся только баланс и строка поиска.
 * Замки тренера в ответе API — подсказка интерфейсу; ставку всё равно проверяет
 * `database.place_outright_bet`.
 */

import { api } from './api.js';
import { store } from './store.js';
import { tgBridge } from './tg.js';
import { escapeHtml, renderTeamLogoHtml } from './ui.js';
import { chartCard, pillBar, splineChart, donutChart, legend } from './charts.js';

const GROUPS = [
  { id: 'champion', label: 'Чемпион', icon: '🏅', types: ['division_winner'] },
  { id: 'cup', label: 'Кубки', icon: '🏆', types: ['cup_winner'] },
  { id: 'scorer', label: 'Бомбардиры', icon: '⚽', types: ['division_top_scorer', 'league_top_scorer'] },
  { id: 'my', label: 'Мои ставки', icon: '🎫', types: [] },
];

const MARKET_STATUS = {
  open: 'Приём открыт',
  suspended: 'Пауза',
  settled: 'Рассчитан',
  voided: 'Аннулирован',
};

const BET_STATUS = {
  pending: ['В игре', 'pending'],
  won: ['Выигрыш', 'won'],
  lost: ['Проигрыш', 'lost'],
  refunded: ['Возврат', 'refunded'],
  void: ['Возврат', 'refunded'],
  voided: ['Возврат', 'refunded'],
};

const LIST_PREVIEW = 12;
const DONUT_SLICES = 4;
const HISTORY_TOP = 4;
const QUICK_STAKES = [50, 100, 250, 500];

const pct = (p) => {
  const v = Number(p || 0) * 100;
  if (v > 0 && v < 1) return '<1';
  return v >= 10 ? Math.round(v).toString() : (Math.round(v * 10) / 10).toString();
};
const fmtOdd = (o) => Number(o || 0).toFixed(2);
const coins = (n) => `${Math.round(Number(n) || 0).toLocaleString('ru-RU')} 🪙`;
const freebetTotal = (list) => coins(list.reduce((acc, f) => acc + Number(f.amount || 0), 0));
// Фрибеты по сумме: {id самого старого, amount, count}. Список уже отсортирован по id.
const freebetOptions = (list) => {
  const byAmount = new Map();
  for (const f of list) {
    const amount = Number(f.amount || 0);
    const entry = byAmount.get(amount);
    if (entry) entry.count += 1;
    else byAmount.set(amount, { id: f.id, amount, count: 1 });
  }
  return [...byAmount.values()].sort((a, b) => a.amount - b.amount);
};
const hhmm = (t) => {
  const m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(t || ''));
  return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]}` : '';
};
const shortDate = (t) => {
  const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(t || ''));
  return m ? `${m[3]}.${m[2]}` : '';
};

class OutrightsView {
  constructor() {
    this.container = null;
    this.board = null;
    this.loading = false;
    this.error = null;
    this.group = 'champion';
    this.picked = {};        // group → market id
    this.expanded = new Set(); // рынки, где раскрыт весь список
    this.history = new Map();  // market id → {series} | 'loading' | 'error'
    this.my = null;
    this.myLoading = false;
    this.query = '';
    this.sheet = null;
    this._bound = false;
  }

  mount() {
    this.container = document.getElementById('outrights-view-container');
    if (!this.container || this._bound) return;
    this._bound = true;
    this.container.addEventListener('click', (e) => this.onClick(e));
  }

  async open() {
    this.mount();
    this.render();
    await this.loadBoard();
  }

  setQuery(q) {
    const next = (q || '').trim().toLowerCase();
    if (next === this.query) return;
    this.query = next;
    if (this.board) this.render();
  }

  // ─── Данные ─────────────────────────────────────────────────────────────

  async loadBoard() {
    this.loading = true;
    this.error = null;
    this.render();
    try {
      const res = await api.getOutrights();
      this.board = res;
    } catch (e) {
      this.error = e.message || 'Не удалось загрузить рынки';
    } finally {
      this.loading = false;
    }
    this.render();
    if (this.group === 'my') this.loadMy();
  }

  async loadHistory(marketId, force = false) {
    const cached = this.history.get(marketId);
    if (!force && cached && cached !== 'error') return;
    this.history.set(marketId, 'loading');
    try {
      const res = await api.getOutrightHistory(marketId, HISTORY_TOP);
      this.history.set(marketId, { series: res.series || [] });
    } catch (_) {
      this.history.set(marketId, 'error');
    }
    // Пока грузилась история, игрок мог уйти на другой рынок.
    if (this.currentMarket()?.id === marketId) this.renderTrend(marketId);
  }

  async loadMy() {
    this.myLoading = true;
    this.render();
    try {
      this.my = await api.getMyOutrightBets();
    } catch (e) {
      this.my = { error: e.message || 'Не удалось загрузить ставки', bets: [] };
    } finally {
      this.myLoading = false;
    }
    if (this.group === 'my') this.render();
  }

  groupMarkets(groupId) {
    const g = GROUPS.find(x => x.id === groupId);
    const markets = (this.board?.markets || []).filter(m => g && g.types.includes(m.type));
    // Общие рынки (кубок, вся лига) — первыми, затем дивизионы по порядку.
    return markets.sort((a, b) => (a.division_id || 0) - (b.division_id || 0));
  }

  currentMarket() {
    if (this.group === 'my') return null;
    const markets = this.groupMarkets(this.group);
    if (!markets.length) return null;
    const picked = markets.find(m => m.id === this.picked[this.group]);
    if (picked) return picked;
    // По умолчанию — рынок своего дивизиона, если он открыт для ставок, иначе первый.
    const own = markets.find(m => m.division_id && m.division_id === store.state.selectedDivisionId && !m.locked);
    return own || markets.find(m => !m.locked) || markets[0];
  }

  scopeLabel(m) {
    if (!m.division_id) return m.type === 'cup_winner' ? '🏆 Общий' : '🌐 Вся лига';
    const d = (this.board?.divisions || []).find(x => x.id === m.division_id);
    return d?.name || `Дивизион ${m.division_id}`;
  }

  // ─── Отрисовка ─────────────────────────────────────────────────────────

  render() {
    if (!this.container) return;
    const tabs = `
      <div class="ob-groups scroll-row">
        ${GROUPS.map(g => `
          <button class="ob-group-btn ${g.id === this.group ? 'active' : ''}" data-ob-group="${g.id}">
            <span>${g.icon}</span>${escapeHtml(g.label)}
            ${g.id === 'my' && this.board?.open_bets ? `<span class="ob-count">${this.board.open_bets}</span>` : ''}
          </button>`).join('')}
      </div>`;

    if (this.loading && !this.board) {
      this.container.innerHTML = `${tabs}${this.skeleton()}`;
      return;
    }
    if (this.error && !this.board) {
      this.container.innerHTML = `${tabs}
        <div class="cup-empty cup-error">
          <div class="cup-empty-title">${escapeHtml(this.error)}</div>
          <button class="ob-btn" data-ob-reload>Повторить</button>
        </div>`;
      return;
    }
    if (this.group === 'my') {
      this.container.innerHTML = tabs + this.renderMy();
      return;
    }

    const markets = this.groupMarkets(this.group);
    if (!markets.length) {
      this.container.innerHTML = `${tabs}
        <div class="cup-empty">
          <div class="cup-empty-icon">📈</div>
          <div class="cup-empty-title">Рынков пока нет</div>
          <div>Линия появится, когда начнётся сезон и сформируются составы.</div>
        </div>`;
      return;
    }
    const market = this.currentMarket();
    const scopes = markets.length > 1 ? `
      <div class="cup-stage-chips scroll-row">
        ${markets.map(m => `
          <button class="cup-stage-chip ${m.id === market.id ? 'active' : ''}" data-ob-market="${m.id}">
            ${m.locked ? '🔒 ' : ''}${escapeHtml(this.scopeLabel(m))}
          </button>`).join('')}
      </div>` : '';

    this.container.innerHTML = `${tabs}${scopes}${this.renderMarket(market)}`;
    this.loadHistory(market.id);
  }

  skeleton() {
    return `
      <div class="ob-hero">
        <div class="mono-card ob-skeleton"></div>
        <div class="mono-card ob-skeleton"></div>
      </div>
      <div class="mono-card ob-skeleton tall"></div>`;
  }

  renderMarket(m) {
    const selections = m.selections || [];
    const live = selections.filter(s => ['active', 'suspended', 'won'].includes(s.status));
    const ranked = [...live].sort((a, b) => b.probability - a.probability);
    const fav = ranked.find(s => s.key !== '__other__') || ranked[0];

    let banner = '';
    if (m.status === 'settled') {
      const winners = selections.filter(s => s.status === 'won').map(s => s.name);
      banner = `<div class="ob-banner won">🏆 Рынок рассчитан${winners.length ? `: ${escapeHtml(winners.join(', '))}` : ''}</div>`;
    } else if (m.status === 'voided') {
      banner = `<div class="ob-banner">↩️ Рынок аннулирован, ставки возвращены${m.void_reason ? ` — ${escapeHtml(m.void_reason)}` : ''}</div>`;
    } else if (m.status === 'suspended') {
      banner = `<div class="ob-banner">⏸ Приём ставок на рынок временно остановлен</div>`;
    }
    if (m.locked) banner += `<div class="ob-banner lock">🔒 ${escapeHtml(m.locked)}</div>`;
    const freebets = this.board?.freebets || [];
    if (m.status === 'open' && !m.locked && freebets.length) {
      banner += `<div class="ob-banner gift">🎁 ${freebets.length === 1 ? 'Есть фрибет' : `Фрибетов: ${freebets.length}`} на ${freebetTotal(freebets)} — выберите его в листе ставки</div>`;
    }

    return `
      <div class="ob-market" data-market-id="${m.id}">
        <div class="ob-market-title">${escapeHtml(m.title)}</div>
        ${banner}
        <div class="ob-hero">
          ${this.renderShareCard(m, ranked, fav)}
          <div class="ob-trend-slot" id="ob-trend-${m.id}">${this.trendCard(m)}</div>
        </div>
        ${this.renderLine(m, selections)}
      </div>`;
  }

  renderShareCard(m, ranked, fav) {
    if (!fav) {
      return chartCard({ label: 'Расклад шансов', chip: MARKET_STATUS[m.status] || m.status, chart: '<div class="mono-empty">Нет претендентов</div>' });
    }
    const top = ranked.slice(0, DONUT_SLICES);
    const rest = ranked.slice(DONUT_SLICES).reduce((acc, s) => acc + Number(s.probability || 0), 0);
    const segs = top.map((s, i) => ({ value: s.probability, label: `${s.name} — ${pct(s.probability)}%`, highlight: i === 0 }));
    if (rest > 0.001) segs.push({ value: rest, label: `Остальные — ${pct(rest)}%` });
    const legendItems = top.map((s, i) => ({ label: s.name, value: `${pct(s.probability)}%`, highlight: i === 0 }));
    if (rest > 0.001) legendItems.push({ label: 'Остальные', value: `${pct(rest)}%` });
    const alive = (m.selections || []).filter(s => s.status === 'active' || s.status === 'suspended').length;
    return chartCard({
      label: 'Расклад шансов',
      chip: MARKET_STATUS[m.status] || m.status,
      value: pct(fav.probability),
      unit: `% · ${fav.name}`,
      chart: `<div class="ob-donut-row">${donutChart(segs, { center: fmtOdd(fav.odds), centerSub: 'кэф' })}${legend(legendItems)}</div>`,
      footLeft: `${alive} в борьбе`,
      footRight: m.priced_at ? `пересчёт ${hhmm(m.priced_at)}` : '',
    });
  }

  trendCard(m) {
    const h = this.history.get(m.id);
    if (!h || h === 'loading') {
      return chartCard({ label: 'Динамика шансов', chip: `топ-${HISTORY_TOP}`, chart: '<div class="mono-empty ob-pulse">Загружаем историю…</div>' });
    }
    if (h === 'error') {
      return chartCard({ label: 'Динамика шансов', chip: `топ-${HISTORY_TOP}`, chart: '<div class="mono-empty"><button class="ob-btn small" data-ob-history-retry>Повторить</button></div>' });
    }
    const series = (h.series || []).filter(s => (s.points || []).length);
    if (!series.length) {
      return chartCard({ label: 'Динамика шансов', chip: `топ-${HISTORY_TOP}`, chart: '<div class="mono-empty">История появится после первых пересчётов</div>' });
    }
    const lead = series[0];
    const pts = lead.points;
    const first = Number(pts[0].prob || 0);
    const last = Number(pts[pts.length - 1].prob || 0);
    const delta = (last - first) * 100;
    const arrow = Math.abs(delta) < 0.05 ? '•' : (delta > 0 ? '▲' : '▼');
    const chart = splineChart(series.map((s, i) => ({
      name: s.name,
      highlight: i === 0,
      points: (s.points || []).map(p => ({ t: p.t, v: Number(p.prob || 0) * 100 })),
    })));
    const allPts = series.flatMap(s => s.points || []);
    const times = allPts.map(p => String(p.t)).sort();
    return chartCard({
      label: 'Динамика шансов',
      chip: `топ-${series.length}`,
      value: pct(last),
      unit: `% ${arrow} ${Math.abs(delta).toFixed(1)} п.п. · ${lead.name}`,
      chart: chart + legend(series.map((s, i) => {
        const p = s.points[s.points.length - 1];
        return { label: s.name, value: fmtOdd(p?.odds), highlight: i === 0 };
      })),
      footLeft: shortDate(times[0]),
      footRight: shortDate(times[times.length - 1]),
    });
  }

  renderTrend(marketId) {
    const slot = document.getElementById(`ob-trend-${marketId}`);
    const m = (this.board?.markets || []).find(x => x.id === marketId);
    if (slot && m) slot.innerHTML = this.trendCard(m);
  }

  renderLine(m, selections) {
    const q = this.query;
    const order = { won: 0, active: 1, suspended: 2, lost: 3, eliminated: 4 };
    let rows = [...selections].sort((a, b) =>
      (order[a.status] ?? 5) - (order[b.status] ?? 5) || b.probability - a.probability);
    if (q) {
      rows = rows.filter(s => `${s.name} ${s.team_name || ''}`.toLowerCase().includes(q));
    }
    const full = this.expanded.has(m.id) || q;
    const shown = full ? rows : rows.slice(0, LIST_PREVIEW);
    const isScorer = m.type.endsWith('top_scorer');
    const maxProb = Math.max(...rows.map(s => Number(s.probability || 0)), 0.0001);

    const list = shown.map((s, idx) => this.selectionRow(m, s, idx, isScorer, maxProb)).join('');
    const more = !full && rows.length > LIST_PREVIEW
      ? `<button class="ob-more" data-ob-expand="${m.id}">Показать всех · ${rows.length}</button>` : '';
    const empty = rows.length === 0
      ? `<div class="mono-empty">${q ? `Нет исходов по запросу «${escapeHtml(q)}»` : 'Исходов пока нет'}</div>` : '';
    return `
      <div class="mono-card ob-line">
        <div class="mono-card-head">
          <span class="mono-label">Линия</span>
          <span class="mono-chip">${rows.length} ${isScorer ? 'игроков' : 'клубов'}</span>
        </div>
        ${empty}${list}${more}
      </div>`;
  }

  selectionRow(m, s, idx, isScorer, maxProb) {
    const done = ['eliminated', 'lost'].includes(s.status);
    const won = s.status === 'won';
    const canBet = m.status === 'open' && s.status === 'active' && !s.locked;
    const logo = s.team_name ? renderTeamLogoHtml(s.team_name, 26) : '<span class="ob-other-icon">＋</span>';
    const sub = isScorer && s.team_name && s.team_name !== s.name ? `<span class="ob-sel-sub">${escapeHtml(s.team_name)}</span>` : '';
    let tag = '';
    if (won) tag = `<span class="ob-tag won">${s.settle_factor && s.settle_factor < 1 ? `делёж ×${Number(s.settle_factor).toFixed(2)}` : 'победа'}</span>`;
    else if (s.status === 'eliminated') tag = '<span class="ob-tag">выбыл</span>';
    else if (s.status === 'suspended') tag = '<span class="ob-tag">пауза</span>';

    let button;
    if (canBet) {
      button = `
        <button class="ob-odd" data-ob-bet="${s.id}">
          <span class="ob-odd-val">${fmtOdd(s.odds)}</span>
        </button>`;
    } else {
      const reason = s.locked || (done ? 'Исход разыгран' : 'Приём ставок закрыт');
      button = `<span class="ob-odd locked" title="${escapeHtml(reason)}">${s.locked ? '🔒' : fmtOdd(s.odds)}</span>`;
    }
    const barPct = (Number(s.probability || 0) / maxProb) * 100;
    return `
      <div class="ob-sel ${done ? 'done' : ''} ${won ? 'won' : ''}">
        <span class="ob-rank">${idx + 1}</span>
        <span class="ob-sel-logo">${logo}</span>
        <div class="ob-sel-main">
          <div class="ob-sel-name"><span>${escapeHtml(s.name)}</span>${tag}</div>
          ${sub}
          <div class="ob-sel-bar">${pillBar(barPct, { highlight: idx === 0 && !done, muted: done })}<span class="ob-sel-pct">${pct(s.probability)}%</span></div>
        </div>
        ${button}
      </div>`;
  }

  renderMy() {
    if (this.myLoading && !this.my) return this.skeleton();
    const bets = this.my?.bets || [];
    if (this.my?.error) {
      return `<div class="cup-empty cup-error"><div class="cup-empty-title">${escapeHtml(this.my.error)}</div>
        <button class="ob-btn" data-ob-my-reload>Повторить</button></div>`;
    }
    const open = bets.filter(b => b.status === 'pending');
    const stake = open.reduce((a, b) => a + Number(b.amount || 0), 0);
    const potential = open.reduce((a, b) => a + Number(b.potential_win || 0), 0);
    const max = this.my?.max_open_bets || this.board?.max_open_bets || 20;
    const settled = bets.filter(b => b.status !== 'pending');
    const won = settled.filter(b => b.status === 'won').length;
    const lost = settled.filter(b => b.status === 'lost').length;
    const refunded = settled.length - won - lost;

    const summary = chartCard({
      label: 'Мои долгосрочные',
      chip: `${open.length} из ${max}`,
      value: potential.toLocaleString('ru-RU'),
      unit: ' 🪙 к выплате',
      chart: `<div class="ob-donut-row">${donutChart([
        { value: open.length, label: `В игре — ${open.length}`, highlight: true },
        { value: won, label: `Выигрыш — ${won}` },
        { value: lost, label: `Проигрыш — ${lost}` },
        { value: refunded, label: `Возврат — ${refunded}` },
      ], { size: 112, thickness: 12, center: String(bets.length), centerSub: 'ставок' })}
      ${legend([
        { label: 'В игре', value: open.length, highlight: true },
        { label: 'Выигрыш', value: won },
        { label: 'Проигрыш', value: lost },
        { label: 'Возврат', value: refunded },
      ])}</div>`,
      footLeft: `в игре ${coins(stake)}`,
      footRight: `лимит ${max} ставок`,
    });

    if (!bets.length) {
      return `${summary}
        <div class="cup-empty">
          <div class="cup-empty-icon">🎫</div>
          <div class="cup-empty-title">Долгосрочных ставок пока нет</div>
          <div>Выберите чемпиона, обладателя кубка или бомбардира сезона.</div>
        </div>`;
    }
    const rows = bets.map(b => {
      const [label, cls] = BET_STATUS[b.status] || [b.status, ''];
      const payout = b.status === 'pending'
        ? `<span class="ob-bet-win">→ ${coins(b.potential_win)}</span>`
        : `<span class="ob-bet-win ${cls}">${b.actual_payout != null ? coins(b.actual_payout) : ''}</span>`;
      const drift = b.status === 'pending' && b.current_odd && Math.abs(b.current_odd - b.odd) >= 0.01
        ? `<span class="ob-drift ${b.current_odd < b.odd ? 'up' : 'down'}" title="Текущий коэффициент">сейчас ${fmtOdd(b.current_odd)}</span>` : '';
      const dh = b.dead_heat_factor && b.dead_heat_factor < 1 ? ` · делёж ×${Number(b.dead_heat_factor).toFixed(2)}` : '';
      return `
        <div class="ob-bet">
          <div class="ob-bet-top">
            <span class="ob-bet-market">${escapeHtml(b.market_title || '')}</span>
            <span class="ob-status ${cls}">${escapeHtml(label)}</span>
          </div>
          <div class="ob-bet-pick">${escapeHtml(b.selection_name || '')} <b>${fmtOdd(b.odd)}</b> ${drift}</div>
          <div class="ob-bet-foot">
            <span>#${b.id} · ${hhmm(b.created_at)} · ${b.freebet_id ? `<span class="ob-tag gift">фрибет</span> ` : ''}${coins(b.amount)}${dh}</span>
            ${payout}
          </div>
        </div>`;
    }).join('');
    return `${summary}<div class="mono-card ob-line">${rows}</div>`;
  }

  // ─── События ───────────────────────────────────────────────────────────

  async onClick(e) {
    const group = e.target.closest('[data-ob-group]');
    if (group) {
      tgBridge.hapticImpact('light');
      this.group = group.dataset.obGroup;
      this.render();
      if (this.group === 'my') this.loadMy();
      return;
    }
    const market = e.target.closest('[data-ob-market]');
    if (market) {
      tgBridge.hapticImpact('light');
      this.picked[this.group] = parseInt(market.dataset.obMarket);
      this.render();
      return;
    }
    const expand = e.target.closest('[data-ob-expand]');
    if (expand) {
      this.expanded.add(parseInt(expand.dataset.obExpand));
      this.render();
      return;
    }
    if (e.target.closest('[data-ob-reload]')) { await this.loadBoard(); return; }
    if (e.target.closest('[data-ob-my-reload]')) { await this.loadMy(); return; }
    if (e.target.closest('[data-ob-history-retry]')) {
      const m = this.currentMarket();
      if (m) this.loadHistory(m.id, true);
      return;
    }
    const bet = e.target.closest('[data-ob-bet]');
    if (bet) {
      tgBridge.hapticImpact('medium');
      this.openSheet(parseInt(bet.dataset.obBet));
    }
  }

  findSelection(selectionId) {
    for (const m of (this.board?.markets || [])) {
      const s = (m.selections || []).find(x => x.id === selectionId);
      if (s) return { market: m, selection: s };
    }
    return null;
  }

  // ─── Лист ставки ───────────────────────────────────────────────────────

  ensureSheet() {
    let overlay = document.getElementById('ob-sheet-overlay');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'ob-sheet-overlay';
    overlay.className = 'modal-overlay ob-sheet-overlay';
    overlay.innerHTML = '<div class="modal-content ob-sheet"></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay || e.target.closest('[data-ob-close]')) { this.closeSheet(); return; }
      const quick = e.target.closest('[data-ob-stake]');
      if (quick) {
        const input = overlay.querySelector('.ob-stake-input');
        const v = quick.dataset.obStake === 'max' ? this.maxStake() : parseInt(quick.dataset.obStake);
        input.value = String(Math.max(0, v));
        this.updateSheetTotals();
        tgBridge.hapticImpact('light');
        return;
      }
      const fund = e.target.closest('[data-ob-fund]');
      if (fund) {
        if (this.sheet?.busy) return;
        this.setFunding(fund.dataset.obFund === 'coins' ? null : parseInt(fund.dataset.obFund));
        tgBridge.hapticImpact('light');
        return;
      }
      if (e.target.closest('[data-ob-confirm]')) this.submitSheet();
    });
    overlay.addEventListener('input', (e) => {
      if (e.target.classList.contains('ob-stake-input')) this.updateSheetTotals();
    });
    return overlay;
  }

  maxStake() {
    const balance = Math.floor(Number(store.state.user?.balance || 0));
    const maxBet = Number(store.state.user?.bet_limits?.max_bet || 0);
    return maxBet > 0 ? Math.min(balance, maxBet) : balance;
  }

  openSheet(selectionId) {
    const found = this.findSelection(selectionId);
    if (!found) return;
    const { market, selection } = found;
    this.sheet = {
      selectionId,
      odd: Number(selection.odds),
      key: `ob-${selectionId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      busy: false,
      freebet: null, // {id, amount} — ставка фрибетом вместо монет
    };
    const overlay = this.ensureSheet();
    const minBet = this.board?.min_bet || 10;
    const balance = Math.floor(Number(store.state.user?.balance || 0));
    const preset = Math.min(Math.max(minBet, 100), Math.max(minBet, balance));
    overlay.querySelector('.ob-sheet').innerHTML = `
      <div class="ob-sheet-head">
        <span class="mono-label">${escapeHtml(market.title)}</span>
        <button class="ob-sheet-close" data-ob-close aria-label="Закрыть">✕</button>
      </div>
      <div class="ob-sheet-pick">
        <span class="ob-sel-logo">${selection.team_name ? renderTeamLogoHtml(selection.team_name, 34) : ''}</span>
        <div class="ob-sheet-pick-main">
          <div class="ob-sheet-name">${escapeHtml(selection.name)}</div>
          <div class="ob-sel-sub">шанс по модели ${pct(selection.probability)}%</div>
        </div>
        <div class="ob-sheet-odd">${fmtOdd(selection.odds)}</div>
      </div>
      <div class="ob-sheet-notice" hidden></div>
      <div class="ob-fund" hidden></div>
      <div class="ob-fund-note" hidden></div>
      <label class="ob-stake">
        <span>Сумма ставки</span>
        <input class="ob-stake-input" type="number" inputmode="numeric" min="${minBet}" step="1" value="${preset}">
      </label>
      <div class="ob-quick">
        ${QUICK_STAKES.map(v => `<button class="ob-chip" data-ob-stake="${v}">${v}</button>`).join('')}
        <button class="ob-chip" data-ob-stake="max">Макс</button>
      </div>
      <div class="ob-sheet-totals">
        <span>Баланс: <b>${coins(balance)}</b></span>
        <span>Выигрыш: <b class="ob-sheet-win">—</b></span>
      </div>
      <div class="ob-sheet-hint">Расчёт в конце сезона. При делёже первого места выплата — по доле.</div>
      <div class="ob-sheet-hint ob-fund-hint" hidden>Фрибет не списывает баланс: при выигрыше приходит только чистая прибыль, сама сумма фрибета не возвращается. При аннулировании рынка фрибет вернётся.</div>
      <div class="ob-sheet-error"></div>
      <button class="ob-confirm" data-ob-confirm>Поставить</button>`;
    overlay.classList.add('active');
    this.renderFunding();
    this.updateSheetTotals();
  }

  // Выбор оплаты: монеты или фрибет. Одинаковые по сумме фрибеты — одна
  // кнопка, тратится самый старый из них.
  renderFunding() {
    const row = document.querySelector('#ob-sheet-overlay .ob-fund');
    if (!row || !this.sheet) return;
    const options = freebetOptions(this.board?.freebets || []);
    row.hidden = !options.length;
    const current = this.sheet.freebet ? String(this.sheet.freebet.id) : 'coins';
    row.innerHTML = options.length ? `
      <button class="ob-chip ${current === 'coins' ? 'active' : ''}" data-ob-fund="coins">🪙 Монеты</button>
      ${options.map(f => `<button class="ob-chip ${current === String(f.id) ? 'active' : ''}" data-ob-fund="${f.id}">🎁 Фрибет ${f.amount}${f.count > 1 ? ` ×${f.count}` : ''}</button>`).join('')}` : '';
  }

  closeSheet() {
    const overlay = document.getElementById('ob-sheet-overlay');
    if (overlay) overlay.classList.remove('active');
    this.sheet = null;
  }

  updateSheetTotals() {
    const overlay = document.getElementById('ob-sheet-overlay');
    if (!overlay || !this.sheet) return;
    const freebet = this.sheet.freebet;
    const amount = freebet ? freebet.amount : parseInt(overlay.querySelector('.ob-stake-input').value) || 0;
    // Фрибет платит только чистую прибыль — так же считает place_outright_bet.
    const win = freebet ? Math.round(amount * (this.sheet.odd - 1)) : Math.round(amount * this.sheet.odd);
    overlay.querySelector('.ob-sheet-win').textContent = amount > 0 ? coins(win) : '—';
    overlay.querySelector('.ob-sheet-odd').textContent = fmtOdd(this.sheet.odd);
    overlay.querySelector('.ob-sheet-error').textContent = '';
  }

  setFunding(freebetId) {
    const overlay = document.getElementById('ob-sheet-overlay');
    if (!overlay || !this.sheet) return;
    const freebet = freebetId != null
      ? (this.board?.freebets || []).find(f => f.id === freebetId) || null
      : null;
    this.sheet.freebet = freebet ? { id: freebet.id, amount: Number(freebet.amount) } : null;
    const key = freebet ? String(freebet.id) : 'coins';
    overlay.querySelectorAll('[data-ob-fund]').forEach(el => el.classList.toggle('active', el.dataset.obFund === key));
    overlay.querySelector('.ob-sheet').classList.toggle('freebet', !!freebet);
    overlay.querySelector('.ob-fund-hint').hidden = !freebet;
    const note = overlay.querySelector('.ob-fund-note');
    note.hidden = !freebet;
    note.innerHTML = freebet
      ? `Ставка фрибетом: <b>${coins(freebet.amount)}</b>${freebet.source_name ? ` · за «${escapeHtml(freebet.source_name)}»` : ''}`
      : '';
    this.updateSheetTotals();
  }

  sheetNotice(text) {
    const el = document.querySelector('#ob-sheet-overlay .ob-sheet-notice');
    if (!el) return;
    el.hidden = !text;
    el.textContent = text || '';
  }

  async submitSheet() {
    const overlay = document.getElementById('ob-sheet-overlay');
    const sheet = this.sheet;
    if (!overlay || !sheet || sheet.busy) return;
    const freebet = sheet.freebet;
    const amount = freebet ? freebet.amount : parseInt(overlay.querySelector('.ob-stake-input').value) || 0;
    const errorEl = overlay.querySelector('.ob-sheet-error');
    const minBet = this.board?.min_bet || 10;
    if (!freebet && amount < minBet) {
      errorEl.textContent = `Минимальная ставка — ${minBet} 🪙.`;
      return;
    }
    const button = overlay.querySelector('[data-ob-confirm]');
    sheet.busy = true;
    button.disabled = true;
    button.textContent = 'Отправляем…';
    try {
      const res = await api.placeOutrightBet({
        selection_id: sheet.selectionId, amount, odd: sheet.odd, idempotency_key: sheet.key,
        freebet_id: freebet ? freebet.id : undefined,
      });
      if (store.state.user && res.balance != null) store.setUser({ ...store.state.user, balance: res.balance });
      tgBridge.hapticNotification('success');
      this.closeSheet();
      this.toast(`Ставка #${res.bet_id}${res.freebet_id ? ' фрибетом' : ''} принята · выигрыш ${coins(res.potential_win)}`);
      this.my = null;
      await this.loadBoard();
      return;
    } catch (err) {
      const code = err.data?.error || err.code;
      tgBridge.hapticNotification(code === 'ODDS_CHANGED' ? 'warning' : 'error');
      if (code === 'ODDS_CHANGED' && err.data?.new_odd) {
        // Ставка не прошла, ключ не израсходован — можно подтвердить новую цену.
        const old = sheet.odd;
        sheet.odd = Number(err.data.new_odd);
        this.updateSheetTotals();
        this.sheetNotice(`Коэффициент изменился: ${fmtOdd(old)} → ${fmtOdd(sheet.odd)}. Подтвердите ставку ещё раз.`);
        this.loadBoard();
      } else if (code === 'OUTRIGHT_REPRICING') {
        errorEl.textContent = err.message || 'Линия пересчитывается после матча — попробуйте через минуту.';
        this.loadBoard();
      } else {
        errorEl.textContent = err.message || 'Ставка не принята.';
        if (['MARKET_SUSPENDED', 'OUTRIGHT_OWN_SCOPE', 'INVALID_SELECTION'].includes(code)) this.loadBoard();
        if (code === 'FREEBET_UNAVAILABLE') {
          // Фрибет уже потрачен в другой вкладке — убираем его из листа.
          await this.loadBoard();
          if (this.sheet === sheet) {
            sheet.freebet = null;
            this.renderFunding();
            this.setFunding(null);
            errorEl.textContent = err.message || 'Фрибет недоступен.';
          }
        }
      }
    } finally {
      if (this.sheet === sheet) {
        sheet.busy = false;
        button.disabled = false;
        button.textContent = 'Поставить';
      }
    }
  }

  toast(message) {
    let el = document.getElementById('ob-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ob-toast';
      el.className = 'adm-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }
}

export const outrightsView = new OutrightsView();
