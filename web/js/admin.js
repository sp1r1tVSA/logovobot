/**
 * web/js/admin.js
 * Вкладка «Управление» — панель Logovo.bet для админов.
 *
 * Самостоятельный модуль: свои запросы к /api/admin/panel/*, своя разметка
 * внутри #admin-root и своя модалка #admin-modal. Права решает сервер —
 * здесь только прячем то, что админу дивизиона всё равно вернули бы 403.
 */

import { api } from './api.js';
import { tgBridge } from './tg.js';
import { escapeHtml, matchRoundLabel } from './ui.js';

const PANEL = '/api/admin/panel';

const TABS = [
  { id: 'dashboard', label: 'Сводка' },
  { id: 'markets', label: 'Рынки' },
  { id: 'picks', label: 'ИИ-прогноз' },
  { id: 'bets', label: 'Купоны' },
  { id: 'players', label: 'Игроки', globalOnly: true },
  { id: 'limits', label: 'Лимиты' },
  { id: 'risk', label: 'Риски' },
];

const MARKET_STATES = [
  { id: 'active', label: 'Активные' },
  { id: 'closed', label: 'Закрытые' },
  { id: 'finished', label: 'Рассчитанные' },
  { id: 'all', label: 'Все' },
];

// Мин. шанс во вкладке «ИИ-прогноз»: 0 — без ограничения.
const PICK_CHANCES = [0, 50, 60, 70, 80];

const BET_STATUSES = [
  { id: 'all', label: 'Все' },
  { id: 'pending', label: 'В игре' },
  { id: 'won', label: 'Выигрыш' },
  { id: 'lost', label: 'Проигрыш' },
  { id: 'refunded', label: 'Возврат' },
  { id: 'cashed_out', label: 'Кэшаут' },
];

const PLAYER_SORTS = [
  { id: 'balance', label: 'Баланс' },
  { id: 'wagered', label: 'Оборот' },
  { id: 'open', label: 'В игре' },
  { id: 'name', label: 'Имя' },
];

const STATUS_LABELS = {
  open: 'Открыт', suspended: 'Пауза', closed: 'Закрыт', settled: 'Рассчитан', voided: 'Аннулирован',
  pending: 'В игре', won: 'Выигрыш', lost: 'Проигрыш', refunded: 'Возврат',
  cancelled: 'Отменён', cashed_out: 'Кэшаут',
  active: 'Активен', acknowledged: 'Принят', resolved: 'Решён',
};

const LIMIT_LABELS = {
  min_bet: 'Мин. ставка',
  max_bet: 'Макс. ставка',
  max_payout: 'Макс. выплата',
  max_daily_stake: 'Ставки за день',
  max_daily_loss: 'Проигрыш за день',
  max_open_exposure: 'Открытый риск игрока',
  max_open_bets: 'Открытых купонов',
  market_exposure_limit: 'Риск на рынок',
  division_exposure_limit: 'Риск на дивизион',
  global_exposure_limit: 'Риск всей лиги',
  max_express_events: 'Событий в экспрессе',
  initial_balance: 'Стартовый баланс',
};

const LIMIT_HINTS = {
  min_bet: 'Меньше поставить нельзя',
  max_bet: 'Потолок суммы одного купона',
  max_payout: 'Больше купон не выплатит',
  max_daily_stake: 'Сумма ставок игрока за сутки',
  max_daily_loss: 'Сколько игрок может проиграть за сутки',
  max_open_exposure: 'Сумма нерассчитанных ставок игрока',
  max_open_bets: 'Купонов в игре одновременно; экспресс — один купон',
  max_express_events: 'Сколько событий можно собрать в экспресс',
  market_exposure_limit: 'Возможная выплата по одному рынку',
  division_exposure_limit: 'Возможная выплата по дивизиону',
  global_exposure_limit: 'Возможная выплата по всей лиге',
  initial_balance: 'Кошелёк нового игрока; уже созданные не меняются',
};

// Порядок и группы на вкладке «Лимиты». Ключ, которого нет в
// me.limit_keys для уровня, в группе просто не показывается.
const LIMIT_GROUPS = [
  { title: 'Ставки и купон', keys: ['min_bet', 'max_bet', 'max_open_bets', 'max_express_events'] },
  { title: 'Игрок', keys: ['max_payout', 'max_daily_stake', 'max_daily_loss', 'max_open_exposure'] },
  { title: 'Риск лиги', keys: ['market_exposure_limit', 'division_exposure_limit', 'global_exposure_limit'] },
  { title: 'Экономика', keys: ['initial_balance'] },
];

// Сумма в монетах или просто число (события, купоны).
const COUNT_LIMITS = new Set(['max_open_bets', 'max_express_events']);
const limitValue = (key, v) => (v == null || v === '' ? '—' : COUNT_LIMITS.has(key) ? fmt(v) : coins(v));

const TX_LABELS = {
  admin_credit: 'Начисление админом',
  admin_debit: 'Списание админом',
  admin_refund: 'Возврат админом',
  bet_placed: 'Ставка',
  bet_won: 'Выигрыш',
  bet_refund: 'Возврат ставки',
  refund: 'Возврат ставки',
  cashout: 'Кэшаут',
  resettle_payout: 'Перерасчёт: выплата',
  resettle_refund: 'Перерасчёт: возврат',
  resettle_reversal: 'Перерасчёт: откат',
  daily_bonus: 'Ежедневный бонус',
  welcome_bonus: 'Стартовый баланс',
  level_up_reward: 'Новый уровень',
};

const AUDIT_LABELS = {
  market_open: 'Рынок открыт',
  market_suspended: 'Рынок приостановлен',
  market_closed: 'Рынок закрыт',
  market_settled: 'Рынок рассчитан',
  market_voided: 'Рынок аннулирован',
  market_suspend_reason: 'Причина: пауза рынка',
  market_resume_reason: 'Причина: открытие рынка',
  market_close_reason: 'Причина: закрытие рынка',
  market_void_reason: 'Причина: аннулирование рынка',
  market_void_bet_refund: 'Возврат по аннулированному рынку',
  live_market_suspend: 'Лайв: пауза рынка',
  live_market_resume: 'Лайв: открытие рынка',
  live_market_close: 'Лайв: закрытие рынка',
  live_market_void: 'Лайв: аннулирование рынка',
  odds_changed: 'Коэффициент изменён',
  bet_voided: 'Купон аннулирован',
  bet_void_reason: 'Причина аннулирования купона',
  wallet_admin_credit: 'Начисление',
  wallet_admin_debit: 'Списание',
  player_betting_banned: 'Запрет ставок',
  player_betting_unbanned: 'Запрет снят',
  betting_paused: 'Приём остановлен',
  betting_resumed: 'Приём возобновлён',
  limit_set: 'Лимит задан',
  limit_reset: 'Лимит сброшен',
  result_correction: 'Исправление результата',
};

const fmt = (n) => Number(n || 0).toLocaleString('ru-RU');
const coins = (n) => `${fmt(n)} 🪙`;
const odd = (v) => (v == null ? '—' : Number(v).toFixed(2));
const esc = escapeHtml;

function statusBadge(status) {
  return `<span class="adm-badge adm-st-${esc(status)}">${esc(STATUS_LABELS[status] || status)}</span>`;
}

function playerName(row) {
  if (row.username) return `@${row.username}`;
  return `ID ${row.user_id}`;
}

function shortTime(value) {
  if (!value) return '';
  // Время хранится в МСК и показывается как есть: «2026-09-24 18:05:11» → «24.09 18:05».
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]}` : String(value);
}

export class AdminPanel {
  constructor(root, modal) {
    this.root = root;
    this.modal = modal;
    this.me = null;
    this.tab = 'dashboard';
    this.divisionId = null;
    this.loaded = false;
    this.busy = false;

    this.markets = { state: 'active', q: '', offset: 0, items: [], total: 0 };
    this.bets = { status: 'pending', userId: null, offset: 0, items: [], total: 0 };
    this.players = { q: '', sort: 'balance', banned: false, items: [] };
    // markets/oddsMin/oddsMax — применённые (уходят в запрос), draft — ещё не применённые.
    this.picks = { markets: [], oddsMin: '', oddsMax: '', draft: null, minChance: 0, valueOnly: false, res: null };

    this._searchTimer = null;
    this._modalSubmit = null;
    this.bind();
  }

  // ─── Сеть ──────────────────────────────────────────────────────────────

  get(path, params = {}) {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') q.append(k, v);
    });
    const qs = q.toString();
    return api.request(`${path}${qs ? `?${qs}` : ''}`);
  }

  post(path, body = {}) {
    return api.request(path, { method: 'POST', body: JSON.stringify(body) });
  }

  scopeParams() {
    return this.divisionId ? { division_id: this.divisionId } : {};
  }

  // ─── Жизненный цикл ────────────────────────────────────────────────────

  async open() {
    if (!this.loaded) {
      this.root.innerHTML = '<div class="adm-empty">Загрузка панели…</div>';
      try {
        this.me = await this.get(`${PANEL}/me`);
        this.loaded = true;
      } catch (e) {
        this.root.innerHTML = `<div class="adm-empty">${esc(e.message || 'Панель недоступна')}</div>`;
        return;
      }
    }
    this.renderShell();
    this.loadTab();
  }

  async refreshMe() {
    try {
      this.me = await this.get(`${PANEL}/me`);
      this.renderPauseBanner();
    } catch (_) { /* баннер просто останется прежним */ }
  }

  renderShell() {
    const me = this.me;
    const tabs = TABS.filter(t => !t.globalOnly || me.is_global);
    if (!tabs.some(t => t.id === this.tab)) this.tab = 'dashboard';

    const divPills = me.divisions.length > 1 || me.is_global
      ? `<div class="category-pills adm-div-pills">
          <button class="category-pill ${this.divisionId ? '' : 'active'}" data-adm-division="">
            ${me.is_global ? 'Вся лига' : 'Все мои'}
          </button>
          ${me.divisions.map(d => `
            <button class="category-pill ${this.divisionId === d.id ? 'active' : ''}" data-adm-division="${d.id}">${esc(d.name)}</button>
          `).join('')}
        </div>`
      : '';

    this.root.innerHTML = `
      <div class="adm-head">
        <div>
          <div class="adm-title">Управление</div>
          <div class="adm-subtitle">${me.is_global ? 'Главный администратор' : `Админ: ${esc(me.divisions.map(d => d.name).join(', '))}`}</div>
        </div>
        <button class="adm-icon-btn" data-adm-refresh title="Обновить">↻</button>
      </div>
      <div id="adm-pause-banner"></div>
      ${divPills}
      <div class="mc-tabs adm-tabs">
        ${tabs.map(t => `<button class="mc-tab-btn ${t.id === this.tab ? 'active' : ''}" data-adm-tab="${t.id}">${t.label}</button>`).join('')}
      </div>
      <div id="adm-body"></div>
    `;
    this.renderPauseBanner();
  }

  renderPauseBanner() {
    const el = this.root.querySelector('#adm-pause-banner');
    if (!el || !this.me) return;
    const pause = this.me.pause || {};
    const names = new Map(this.me.divisions.map(d => [String(d.id), d.name]));
    const lines = [];
    if (pause.global) {
      lines.push(`Приём ставок остановлен во всей лиге${pause.global.reason ? ` — ${esc(pause.global.reason)}` : ''}`);
    }
    Object.entries(pause.divisions || {}).forEach(([id, entry]) => {
      if (!entry) return;
      lines.push(`Остановлен приём: ${esc(names.get(String(id)) || `дивизион ${id}`)}${entry.reason ? ` — ${esc(entry.reason)}` : ''}`);
    });
    el.innerHTML = lines.length
      ? `<div class="adm-banner adm-banner-danger">⛔ ${lines.join('<br>⛔ ')}</div>`
      : '';
  }

  body() {
    return this.root.querySelector('#adm-body');
  }

  loadTab() {
    const body = this.body();
    if (!body) return;
    body.innerHTML = '<div class="adm-empty">Загрузка…</div>';
    const loaders = {
      dashboard: () => this.loadDashboard(),
      markets: () => this.loadMarkets(true),
      picks: () => this.loadPicks(false),
      bets: () => this.loadBets(true),
      players: () => this.loadPlayers(),
      limits: () => this.loadLimits(),
      risk: () => this.loadRisk(),
    };
    (loaders[this.tab] || loaders.dashboard)().catch(e => {
      if (this.body() === body) {
        body.innerHTML = `<div class="adm-empty">${esc(e.message || 'Не удалось загрузить')}</div>`;
      }
    });
  }

  // ─── Сводка ────────────────────────────────────────────────────────────

  async loadDashboard() {
    const res = await this.get(`${PANEL}/dashboard`, this.scopeParams());
    if (this.tab !== 'dashboard') return;
    const d = res.dashboard;
    const t = d.totals;
    const maxTurnover = Math.max(1, ...d.daily.map(x => x.turnover));
    const ggrClass = t.ggr >= 0 ? 'green' : 'red';

    this.body().innerHTML = `
      <div class="kpi-grid">
        ${this.kpi('Открытый риск', coins(t.pending_liability), 'red', `${fmt(t.pending_count)} купонов в игре`)}
        ${this.kpi('Ставки в игре', coins(t.pending_stake), 'gold')}
        ${this.kpi('GGR (доход)', coins(t.ggr), ggrClass, `маржа ${t.margin_pct ?? 0}%`)}
        ${this.kpi('Выплачено', coins(t.paid_out), '', `из ${coins(t.settled_stake)} ставок`)}
        ${this.kpi('Оборот сегодня', coins(d.periods.today.turnover), '', `${fmt(d.periods.today.bets)} купонов · ${fmt(d.periods.today.bettors)} игроков`)}
        ${this.kpi('Оборот 7 дней', coins(d.periods.week.turnover), '', `GGR ${coins(d.periods.week.ggr)}`)}
        ${this.kpi('Всего купонов', fmt(t.total_bets), '', `${fmt(t.bettors)} игроков ставили`)}
        ${this.kpi('Риск-алерты', fmt(d.alerts.active), d.alerts.high ? 'red' : '', d.alerts.high ? `${fmt(d.alerts.high)} высокого уровня` : 'нет срочных')}
      </div>

      <div class="adm-card">
        <div class="adm-card-title">Оборот за 14 дней</div>
        <div class="adm-bars">
          ${d.daily.map(x => `
            <div class="adm-bar" title="${esc(x.day)}: ${coins(x.turnover)}, ${fmt(x.bets)} купонов">
              <div class="adm-bar-fill" style="height:${Math.round((x.turnover / maxTurnover) * 100)}%"></div>
              <span>${esc(String(x.day).slice(8, 10))}</span>
            </div>`).join('')}
        </div>
      </div>

      <div class="adm-card">
        <div class="adm-card-title">Рынки</div>
        <div class="adm-chips">
          ${Object.entries(d.markets).map(([s, n]) => `<span class="adm-chip">${statusBadge(s)} <b>${fmt(n)}</b></span>`).join('')}
        </div>
        <div class="adm-chips adm-mt">
          <span class="adm-chip">Выигрыш <b>${fmt(t.count_won)}</b></span>
          <span class="adm-chip">Проигрыш <b>${fmt(t.count_lost)}</b></span>
          <span class="adm-chip">Возврат <b>${fmt(t.count_refunded)}</b></span>
          <span class="adm-chip">Кэшаут <b>${fmt(t.count_cashed_out)}</b></span>
        </div>
      </div>

      ${d.economy ? `
        <div class="adm-card">
          <div class="adm-card-title">Экономика</div>
          <div class="adm-kv"><span>Монет на кошельках</span><b>${coins(d.economy.coins_in_wallets)}</b></div>
          <div class="adm-kv"><span>Кошельков</span><b>${fmt(d.economy.wallets)}</b></div>
          <div class="adm-kv"><span>Запрет ставок</span><b>${fmt(d.economy.banned_players)}</b></div>
        </div>` : ''}

      <div class="adm-card">
        <div class="adm-card-title">Крупнейшие открытые купоны</div>
        ${d.top_liability.length ? d.top_liability.map(b => `
          <button class="adm-row" data-adm-bet="${b.id}">
            <div class="adm-row-main">
              <b>#${b.id}</b> ${esc(playerName(b))}
              <small>${b.bet_type === 'express' ? 'Экспресс' : 'Ординар'} · ${coins(b.amount)} × ${odd(b.total_odd)}</small>
            </div>
            <div class="adm-row-side red">${coins(b.potential_win)}</div>
          </button>`).join('') : '<div class="adm-muted">Открытых купонов нет</div>'}
      </div>

      <div class="adm-card">
        <div class="adm-card-title">Самые прибыльные игроки</div>
        ${d.top_winners.length ? d.top_winners.map(w => `
          <button class="adm-row" ${this.me.is_global ? `data-adm-player="${w.user_id}"` : 'disabled'}>
            <div class="adm-row-main">${esc(playerName(w))}<small>${esc(w.user_team || '')} · ${fmt(w.bets)} купонов</small></div>
            <div class="adm-row-side ${w.net_profit >= 0 ? 'green' : 'red'}">${w.net_profit >= 0 ? '+' : ''}${coins(w.net_profit)}</div>
          </button>`).join('') : '<div class="adm-muted">Рассчитанных купонов пока нет</div>'}
      </div>
    `;
  }

  kpi(label, value, cls = '', hint = '') {
    return `
      <div class="kpi-card">
        <span class="kpi-label">${esc(label)}</span>
        <span class="kpi-value ${cls}">${esc(value)}</span>
        ${hint ? `<span class="adm-kpi-hint">${esc(hint)}</span>` : ''}
      </div>`;
  }

  // ─── Рынки ─────────────────────────────────────────────────────────────

  async loadMarkets(reset = false) {
    const m = this.markets;
    if (reset) {
      m.offset = 0;
      m.items = [];
      this.body().innerHTML = `
        <div class="category-pills">
          ${MARKET_STATES.map(s => `<button class="category-pill ${m.state === s.id ? 'active' : ''}" data-adm-mstate="${s.id}">${s.label}</button>`).join('')}
        </div>
        <input class="match-search-input adm-input" id="adm-market-search" type="search"
               placeholder="Поиск: команда или номер матча" value="${esc(m.q)}">
        <div id="adm-market-list"><div class="adm-empty">Загрузка…</div></div>
      `;
    }
    const res = await this.get(`${PANEL}/markets`, {
      state: m.state, q: m.q, limit: 15, offset: m.offset, ...this.scopeParams(),
    });
    if (this.tab !== 'markets') return;
    m.items = reset ? res.matches : m.items.concat(res.matches);
    m.total = res.total;
    m.offset = m.items.length;
    this.renderMarketList();
  }

  renderMarketList() {
    const list = this.root.querySelector('#adm-market-list');
    if (!list) return;
    const m = this.markets;
    if (!m.items.length) {
      list.innerHTML = '<div class="adm-empty">Матчей с такими рынками нет</div>';
      return;
    }
    list.innerHTML = m.items.map(match => this.matchCard(match)).join('') +
      (m.items.length < m.total
        ? `<button class="adm-more" data-adm-more="markets">Показать ещё (${fmt(m.total - m.items.length)})</button>`
        : '');
  }

  matchCard(match) {
    const score = match.player1_score != null && match.player2_score != null
      ? `<span class="adm-score">${esc(match.player1_score)}:${esc(match.player2_score)}</span>` : '';
    const when = [match.match_date, match.match_time].filter(Boolean).join(' ');
    return `
      <div class="adm-card adm-match">
        <div class="adm-match-head">
          <div>
            <div class="adm-match-teams">${esc(match.team1_name || '—')} — ${esc(match.team2_name || '—')} ${score}</div>
            <small class="adm-muted">#${match.match_id} · ${esc(match.division_name || 'Дивизион 1')} · ${esc(matchRoundLabel(match))}${when ? ` · ${esc(when)}` : ''}${match.live_minute ? ` · ${esc(match.live_minute)}'` : ''}</small>
          </div>
          ${statusBadge(match.match_status)}
        </div>
        ${match.markets.map(mk => this.marketBlock(mk)).join('')}
      </div>`;
  }

  marketBlock(mk) {
    const finished = mk.status === 'settled' || mk.status === 'voided';
    const actions = [];
    if (mk.status === 'open') actions.push(['suspend', '⏸', 'Приостановить']);
    if (mk.status === 'suspended') actions.push(['resume', '▶', 'Открыть']);
    if (mk.status === 'open' || mk.status === 'suspended') actions.push(['close', '🔒', 'Закрыть приём']);
    if (!finished) actions.push(['void', '✖', 'Аннулировать']);
    const editable = mk.status === 'open' || mk.status === 'suspended';

    return `
      <div class="adm-market">
        <div class="adm-market-head">
          <div class="adm-market-name">${esc(mk.market_name)} ${statusBadge(mk.status)}
            ${mk.open_bets ? `<small class="adm-muted">· ${fmt(mk.open_bets)} в игре</small>` : ''}
          </div>
          <div class="adm-market-actions">
            ${actions.map(([a, icon, title]) => `
              <button class="adm-icon-btn ${a === 'void' ? 'danger' : ''}" title="${title}"
                      data-adm-market-action="${a}" data-market-id="${mk.id}" data-market-name="${esc(mk.market_name)}">${icon}</button>`).join('')}
          </div>
        </div>
        <div class="adm-selections">
          ${mk.selections.map(s => `
            <button class="adm-sel ${s.open_liability ? 'loaded' : ''}" ${editable ? '' : 'disabled'}
                    data-adm-selection="${s.id}" data-odds="${s.odds_value}" data-model="${s.model_odds ?? ''}"
                    data-name="${esc(s.selection_name)}">
              <span class="adm-sel-name">${esc(s.selection_name)}</span>
              <span class="adm-sel-odd">${odd(s.odds_value)}</span>
              ${s.open_liability ? `<span class="adm-sel-risk">риск ${fmt(s.open_liability)}</span>` : ''}
            </button>`).join('')}
        </div>
      </div>`;
  }

  askMarketAction(action, marketId, marketName) {
    const titles = {
      suspend: ['Приостановить рынок?', 'Приём ставок на рынок встанет, принятые купоны останутся в игре.'],
      resume: ['Открыть рынок?', 'Рынок снова начнёт принимать ставки.'],
      close: ['Закрыть приём на рынок?', 'Новых ставок не будет, рынок дождётся расчёта.'],
      void: ['Аннулировать рынок?', 'Все купоны с этим рынком получат возврат. Это необратимо.'],
    };
    const [title, desc] = titles[action];
    this.openForm({
      title,
      desc: `«${marketName}» · ${desc}`,
      fields: [{ name: 'reason', label: action === 'void' ? 'Причина (обязательно)' : 'Причина (необязательно)', type: 'text' }],
      submitLabel: action === 'void' ? 'Аннулировать' : 'Подтвердить',
      danger: action === 'void',
      onSubmit: async ({ reason }) => {
        if (action === 'void' && !reason) throw new Error('Укажите причину аннулирования.');
        await this.post(`${PANEL}/markets/${marketId}/action`, { action, reason, confirm: action === 'void' });
        this.toast(action === 'void' ? 'Рынок аннулирован, ставки возвращены' : 'Готово');
        await this.loadMarkets(true);
      },
    });
  }

  askOdds(selectionId, name, current, model) {
    this.openForm({
      title: 'Изменить коэффициент',
      desc: `«${name}» · сейчас ${odd(current)}${model ? ` · модель ${odd(model)}` : ''}`,
      fields: [{ name: 'odds', label: 'Новый коэффициент', type: 'number', step: '0.01', min: '1.01', value: odd(current) }],
      submitLabel: 'Сохранить',
      onSubmit: async ({ odds }) => {
        const value = Number(String(odds).replace(',', '.'));
        if (!(value >= 1.01 && value <= 1000)) throw new Error('Коэффициент — от 1.01 до 1000.');
        await this.post(`${PANEL}/selections/${selectionId}/odds`, { odds: value });
        this.toast(`Коэффициент: ${value.toFixed(2)}`);
        await this.loadMarkets(true);
      },
    });
  }

  // ─── Купоны ────────────────────────────────────────────────────────────

  async loadBets(reset = false) {
    const b = this.bets;
    if (reset) {
      b.offset = 0;
      b.items = [];
      this.body().innerHTML = `
        <div class="category-pills">
          ${BET_STATUSES.map(s => `<button class="category-pill ${b.status === s.id ? 'active' : ''}" data-adm-bstatus="${s.id}">${s.label}</button>`).join('')}
        </div>
        ${b.userId ? `<div class="adm-filter-chip">Игрок ID ${esc(b.userId)} <button data-adm-clear-user>✕</button></div>` : ''}
        <div id="adm-bet-list"><div class="adm-empty">Загрузка…</div></div>
      `;
    }
    const res = await this.get(`${PANEL}/bets`, {
      status: b.status, user_id: b.userId, limit: 20, offset: b.offset, ...this.scopeParams(),
    });
    if (this.tab !== 'bets') return;
    b.items = reset ? res.bets : b.items.concat(res.bets);
    b.total = res.total;
    b.offset = b.items.length;
    this.renderBetList();
  }

  renderBetList() {
    const list = this.root.querySelector('#adm-bet-list');
    if (!list) return;
    const b = this.bets;
    if (!b.items.length) {
      list.innerHTML = '<div class="adm-empty">Купонов нет</div>';
      return;
    }
    list.innerHTML = `<div class="adm-muted adm-mb">Найдено: ${fmt(b.total)}</div>` +
      b.items.map(bet => this.betRow(bet)).join('') +
      (b.items.length < b.total
        ? `<button class="adm-more" data-adm-more="bets">Показать ещё (${fmt(b.total - b.items.length)})</button>`
        : '');
  }

  betRow(bet) {
    const items = bet.items || [];
    const first = items[0];
    const legs = first
      ? `${esc(first.team1_name)} — ${esc(first.team2_name)}${items.length > 1 ? ` и ещё ${items.length - 1}` : ''}`
      : '';
    const payout = bet.status === 'won' || bet.status === 'cashed_out' ? bet.actual_payout : bet.potential_win;
    return `
      <button class="adm-row adm-bet-row" data-adm-bet="${bet.id}">
        <div class="adm-row-main">
          <b>#${bet.id}</b> ${esc(playerName(bet))} ${statusBadge(bet.status)}
          <small>${legs}</small>
          <small>${bet.bet_type === 'express' ? 'Экспресс' : 'Ординар'} · ${coins(bet.amount)} × ${odd(bet.total_odd)} · ${esc(shortTime(bet.created_at))}</small>
        </div>
        <div class="adm-row-side ${bet.status === 'won' ? 'red' : ''}">${coins(payout)}</div>
      </button>`;
  }

  async openBet(betId) {
    this.showModal('<div class="adm-empty">Загрузка купона…</div>');
    let bet;
    try {
      bet = (await this.get(`${PANEL}/bets/${betId}`)).bet;
    } catch (e) {
      this.showModal(this.modalFrame('Купон', `<div class="adm-empty">${esc(e.message)}</div>`));
      return;
    }
    const legs = (bet.items || []).map(it => `
      <div class="adm-leg">
        <div class="adm-leg-top">
          <b>${esc(it.team1_name)} — ${esc(it.team2_name)}</b>
          ${statusBadge(it.status)}
        </div>
        <small class="adm-muted">${esc(it.division_name || 'Дивизион 1')} · ${esc(matchRoundLabel({ ...it, round_number: it.tour }))}
          ${it.player1_score != null ? ` · счёт ${esc(it.player1_score)}:${esc(it.player2_score)}` : ''}</small>
        <div class="adm-leg-pick">${esc(it.market_name || 'Исход')}: <b>${esc(it.selection_name || it.outcome_type)}</b> <span class="adm-sel-odd">${odd(it.odd)}</span></div>
      </div>`).join('');

    const body = `
      <div class="adm-kv"><span>Игрок</span><b>${esc(playerName(bet))}${bet.user_team ? ` · ${esc(bet.user_team)}` : ''}</b></div>
      <div class="adm-kv"><span>Статус</span>${statusBadge(bet.status)}</div>
      <div class="adm-kv"><span>Тип</span><b>${bet.bet_type === 'express' ? 'Экспресс' : 'Ординар'}</b></div>
      <div class="adm-kv"><span>Ставка</span><b>${coins(bet.amount)}</b></div>
      <div class="adm-kv"><span>Коэффициент</span><b>${odd(bet.total_odd)}</b></div>
      <div class="adm-kv"><span>Возможный выигрыш</span><b>${coins(bet.potential_win)}</b></div>
      ${bet.actual_payout ? `<div class="adm-kv"><span>Выплачено</span><b>${coins(bet.actual_payout)}</b></div>` : ''}
      <div class="adm-kv"><span>Создан</span><b>${esc(shortTime(bet.created_at))}</b></div>
      ${bet.settled_at ? `<div class="adm-kv"><span>Рассчитан</span><b>${esc(shortTime(bet.settled_at))}</b></div>` : ''}
      ${bet.user_wallet_balance != null ? `<div class="adm-kv"><span>Баланс игрока</span><b>${coins(bet.user_wallet_balance)}</b></div>` : ''}
      <div class="adm-section-label">События</div>
      ${legs || '<div class="adm-muted">Нет событий</div>'}
      <div class="adm-actions">
        ${this.me.is_global ? `<button class="adm-btn" data-adm-player="${bet.user_id}">Игрок</button>` : ''}
        ${bet.status === 'pending' ? `<button class="adm-btn danger" data-adm-void-bet="${bet.id}" data-amount="${bet.amount}">Аннулировать</button>` : ''}
      </div>`;
    this.showModal(this.modalFrame(`Купон #${bet.id}`, body));
  }

  askVoidBet(betId, amount) {
    this.openForm({
      title: `Аннулировать купон #${betId}?`,
      desc: `Игроку вернётся ${coins(amount)}. Это необратимо.`,
      fields: [{ name: 'reason', label: 'Причина (необязательно)', type: 'text' }],
      submitLabel: 'Аннулировать',
      danger: true,
      onSubmit: async ({ reason }) => {
        await this.post(`${PANEL}/bets/${betId}/void`, { confirm: true, reason });
        this.toast('Купон аннулирован, ставка возвращена');
        if (this.tab === 'bets') await this.loadBets(true);
        else if (this.tab === 'dashboard') await this.loadDashboard();
      },
    });
  }

  // ─── Игроки (только глобальный админ) ──────────────────────────────────

  async loadPlayers() {
    const p = this.players;
    if (!this.root.querySelector('#adm-player-list')) {
      this.body().innerHTML = `
        <input class="match-search-input adm-input" id="adm-player-search" type="search"
               placeholder="Ник, клуб или Telegram ID" value="${esc(p.q)}">
        <div class="category-pills">
          ${PLAYER_SORTS.map(s => `<button class="category-pill ${p.sort === s.id ? 'active' : ''}" data-adm-psort="${s.id}">${s.label}</button>`).join('')}
          <button class="category-pill ${p.banned ? 'active' : ''}" data-adm-pbanned>⛔ Запрет</button>
        </div>
        <div id="adm-player-list"><div class="adm-empty">Загрузка…</div></div>
      `;
    }
    const res = await this.get(`${PANEL}/players`, {
      q: p.q, sort: p.sort, banned: p.banned ? 1 : '', limit: 50,
    });
    if (this.tab !== 'players') return;
    p.items = res.players;
    const list = this.root.querySelector('#adm-player-list');
    if (!list) return;
    list.innerHTML = p.items.length ? p.items.map(pl => `
      <button class="adm-row" data-adm-player="${pl.user_id}">
        <div class="adm-row-main">
          ${esc(playerName(pl))} ${pl.is_banned ? '<span class="adm-badge adm-st-voided">запрет</span>' : ''}
          <small>${esc(pl.team_name || 'без клуба')}${pl.division_name ? ` · ${esc(pl.division_name)}` : ''}</small>
          <small>Оборот ${coins(pl.total_wagered)} · купонов ${fmt(pl.bets_count)}${pl.open_bets ? ` · в игре ${fmt(pl.open_bets)} на ${coins(pl.open_stake)}` : ''}</small>
        </div>
        <div class="adm-row-side gold">${pl.balance == null ? '—' : coins(pl.balance)}</div>
      </button>`).join('') : '<div class="adm-empty">Никого не нашли</div>';
  }

  async openPlayer(userId) {
    this.showModal('<div class="adm-empty">Загрузка игрока…</div>');
    let p;
    try {
      p = (await this.get(`${PANEL}/players/${userId}`)).player;
    } catch (e) {
      this.showModal(this.modalFrame('Игрок', `<div class="adm-empty">${esc(e.message)}</div>`));
      return;
    }
    const w = p.wallet || {};
    const s = p.summary || {};
    const eff = p.effective_limits || {};
    const overrides = p.limit_overrides || {};
    const userKeys = (this.me.limit_keys && this.me.limit_keys.user) || [];

    const body = `
      ${p.ban ? `<div class="adm-banner adm-banner-danger">⛔ Ставки запрещены с ${esc(shortTime(p.ban.banned_at))}${p.ban.reason ? ` — ${esc(p.ban.reason)}` : ''}</div>` : ''}
      <div class="adm-kv"><span>Клуб</span><b>${esc(p.team_name || '—')}${p.division_name ? ` · ${esc(p.division_name)}` : ''}</b></div>
      <div class="adm-kv"><span>Telegram ID</span><b>${esc(p.user_id)}</b></div>
      <div class="adm-kv"><span>Баланс</span><b class="gold">${w.balance == null ? 'нет кошелька' : coins(w.balance)}</b></div>
      <div class="adm-kv"><span>Оборот / выиграно</span><b>${coins(w.total_wagered)} / ${coins(w.total_won)}</b></div>
      <div class="adm-kv"><span>Купонов</span><b>${fmt(s.total_bets)} (в игре ${fmt(s.count_pending)})</b></div>
      <div class="adm-kv"><span>Чистый итог</span><b class="${(s.net_profit || 0) >= 0 ? 'green' : 'red'}">${coins(s.net_profit)}</b></div>

      <div class="adm-actions">
        <button class="adm-btn" data-adm-adjust="${p.user_id}" data-sign="1">+ Начислить</button>
        <button class="adm-btn" data-adm-adjust="${p.user_id}" data-sign="-1">− Списать</button>
        <button class="adm-btn" data-adm-player-bets="${p.user_id}">Купоны</button>
        ${p.ban
          ? `<button class="adm-btn" data-adm-unban="${p.user_id}">Снять запрет</button>`
          : `<button class="adm-btn danger" data-adm-ban="${p.user_id}">Запретить ставки</button>`}
      </div>

      <div class="adm-section-label">Личные лимиты</div>
      ${userKeys.map(k => `
        <button class="adm-row adm-limit-row" data-adm-limit="user" data-scope-id="${p.user_id}" data-key="${k}"
                data-current="${overrides[k] ?? ''}" data-effective="${eff[k] ?? ''}" data-owner="${esc(playerName(p))}">
          <div class="adm-row-main">${esc(LIMIT_LABELS[k] || k)}<small>${esc(LIMIT_HINTS[k] || '')}</small></div>
          <div class="adm-row-side ${overrides[k] != null ? 'gold' : ''}">${limitValue(k, eff[k])}${overrides[k] != null ? ' ✎' : ''}</div>
        </button>`).join('')}

      <div class="adm-section-label">Движение монет</div>
      ${(p.transactions || []).length ? p.transactions.map(tx => `
        <div class="adm-tx">
          <div><b>${esc(TX_LABELS[tx.transaction_type] || tx.transaction_type)}</b>
            <small class="adm-muted">${esc(shortTime(tx.created_at))}${tx.reference_type === 'bet' && tx.reference_id ? ` · купон #${esc(tx.reference_id)}` : ''}</small></div>
          <div class="${tx.amount >= 0 ? 'green' : 'red'}">${tx.amount >= 0 ? '+' : ''}${fmt(tx.amount)}</div>
        </div>`).join('') : '<div class="adm-muted">Операций нет</div>'}
    `;
    this.showModal(this.modalFrame(playerName(p), body));
  }

  askAdjust(userId, sign) {
    const credit = sign > 0;
    this.openForm({
      title: credit ? 'Начислить монеты' : 'Списать монеты',
      desc: credit ? 'Монеты придут на кошелёк игрока.' : 'Баланс не может уйти в минус.',
      fields: [
        { name: 'amount', label: 'Сумма, 🪙', type: 'number', step: '1', min: '1' },
        { name: 'reason', label: 'Причина (обязательно)', type: 'text' },
      ],
      submitLabel: credit ? 'Начислить' : 'Списать',
      danger: !credit,
      onSubmit: async ({ amount, reason }) => {
        const value = parseInt(amount, 10);
        if (!(value > 0)) throw new Error('Сумма должна быть больше нуля.');
        if (!reason) throw new Error('Укажите причину.');
        const res = await this.post(`${PANEL}/players/${userId}/adjust`, { amount: value * sign, reason });
        this.toast(`Баланс: ${coins(res.result.new_balance)}`);
        await this.openPlayer(userId);
        if (this.tab === 'players') this.loadPlayers().catch(() => {});
      },
    });
  }

  askBan(userId) {
    this.openForm({
      title: 'Запретить ставки?',
      desc: 'Игрок не сможет делать новые ставки. Открытые купоны рассчитаются как обычно.',
      fields: [{ name: 'reason', label: 'Причина (обязательно)', type: 'text' }],
      submitLabel: 'Запретить',
      danger: true,
      onSubmit: async ({ reason }) => {
        if (!reason) throw new Error('Укажите причину.');
        await this.post(`${PANEL}/players/${userId}/ban`, { reason });
        this.toast('Ставки запрещены');
        await this.openPlayer(userId);
        if (this.tab === 'players') this.loadPlayers().catch(() => {});
      },
    });
  }

  async unban(userId) {
    try {
      await this.post(`${PANEL}/players/${userId}/unban`, {});
      this.toast('Запрет снят');
      await this.openPlayer(userId);
      if (this.tab === 'players') this.loadPlayers().catch(() => {});
    } catch (e) {
      this.toast(e.message, true);
    }
  }

  // ─── ИИ-прогноз: исходы по шансу захода ─────────────────────────────────

  async loadPicks(refresh = false) {
    const f = this.picks;
    const body = this.body();
    body.innerHTML = `<div class="adm-empty">${refresh ? 'Пересчитываем прогноз…' : 'ИИ анализирует линию…'}<br>
      <small class="adm-muted">Бесплатной модели может понадобиться до минуты.</small></div>`;
    const res = await this.get(`${PANEL}/picks`, {
      ...this.scopeParams(),
      markets: f.markets.join(','),
      odds_min: f.oddsMin,
      odds_max: f.oddsMax,
      ...(refresh ? { refresh: 1 } : {}),
    });
    if (this.tab !== 'picks' || this.body() !== body) return;
    f.res = res;
    f.draft = { markets: [...f.markets], oddsMin: f.oddsMin, oddsMax: f.oddsMax };

    const note = res.source === 'ai'
      ? `Модель: <b>${esc(res.model || '')}</b>`
      : res.error === 'no_key'
        ? 'OPENROUTER_API_KEY не задан — показан расчёт по коэффициентам линии.'
        : 'ИИ сейчас недоступен — показан расчёт по коэффициентам линии.';
    const groups = res.market_groups || [];
    const pill = (attr, active, label) => `<button class="category-pill ${active ? 'active' : ''}" ${attr}>${label}</button>`;

    body.innerHTML = `
      <div class="adm-card">
        <div class="adm-pause-row">
          <div class="adm-row-main">
            <b>От самого уверенного к самому неуверенному</b>
            <small class="adm-picks-note ${res.source === 'ai' ? '' : 'gold'}">${note}<br>
              ${fmt(res.matches_considered)} матчей · ${fmt(res.options_considered)} исходов ·
              обновлено ${esc(shortTime(res.generated_at))}${res.cached ? ' · из кэша' : ''}</small>
          </div>
          <button class="adm-btn small" data-adm-picks-refresh>Пересчитать</button>
        </div>
      </div>
      <div class="adm-card">
        <div class="adm-card-title">Что разбирает ИИ</div>
        <div class="category-pills adm-picks-pills" id="adm-picks-markets">
          ${pill('data-adm-pmarket=""', !f.draft.markets.length, 'Все рынки')}
          ${groups.map(g => pill(`data-adm-pmarket="${esc(g.id)}"`, f.draft.markets.includes(g.id), esc(g.label))).join('')}
        </div>
        <div class="adm-picks-odds">
          <span class="adm-muted">Кэф</span>
          <input class="adm-input" id="adm-picks-odds-min" type="number" inputmode="decimal" step="0.05" min="1"
                 placeholder="от" value="${esc(f.draft.oddsMin)}">
          <span class="adm-muted">—</span>
          <input class="adm-input" id="adm-picks-odds-max" type="number" inputmode="decimal" step="0.05" min="1"
                 placeholder="до" value="${esc(f.draft.oddsMax)}">
          <button class="adm-btn small primary" data-adm-picks-apply disabled>Применить</button>
        </div>
        <small class="adm-muted adm-picks-hint">Рынок и кэф меняют набор исходов для модели — это новый запрос к ИИ.</small>
        <div class="adm-card-title adm-mt">Показывать</div>
        <div class="category-pills adm-picks-pills" id="adm-picks-view">
          ${PICK_CHANCES.map(c => pill(`data-adm-pchance="${c}"`, f.minChance === c, c ? `от ${c}%` : 'Любой шанс')).join('')}
          ${pill('data-adm-pvalue', f.valueOnly, 'Только ценные')}
        </div>
      </div>
      <div class="adm-card" id="adm-picks-list"></div>
      <div class="adm-muted adm-mb">Прогноз — аналитическая оценка, а не гарантия. Исходы с кэфом ниже 1.15 не учитываются,
        не больше двух исходов на матч. «Ценный» — шанс по оценке ИИ выше, чем заложено в кэф.</div>
    `;
    this.renderPicksList();
  }

  renderPicksList() {
    const list = this.root.querySelector('#adm-picks-list');
    const f = this.picks;
    if (!list || !f.res) return;
    const all = f.res.picks || [];
    const picks = all.filter(p => p.probability >= f.minChance && (!f.valueOnly || p.value > 0));
    const probClass = p => (p >= 75 ? 'green' : p >= 55 ? 'gold' : '');
    let empty = 'Открытых рынков для прогноза нет';
    if (all.length) {
      empty = f.valueOnly && f.res.source !== 'ai'
        ? 'Ценные исходы ищет только ИИ: по линии шанс всегда равен заложенному в кэф.'
        : 'Под фильтры ничего не подошло';
    } else if (f.markets.length || f.oddsMin || f.oddsMax) {
      empty = 'Под выбранные рынки и кэф открытых исходов нет';
    }
    list.innerHTML = picks.length ? picks.map((p, i) => `
      <div class="adm-row adm-pick-row">
        <div class="adm-pick-rank">${i + 1}</div>
        <div class="adm-row-main">
          <b>${esc(p.selection_name)}</b> <span class="adm-muted">× ${odd(p.odds)}</span>
          ${p.value > 0 && f.res.source === 'ai' ? '<span class="adm-badge adm-st-open">ценный</span>' : ''}
          <small>${esc(p.team1)} — ${esc(p.team2)} · ${esc(p.division_name || 'Дивизион 1')} · ${esc(matchRoundLabel(p))}</small>
          ${p.reason ? `<small class="adm-pick-reason">${esc(p.reason)}</small>` : ''}
        </div>
        <div class="adm-row-side adm-pick-prob ${probClass(p.probability)}">${Number(p.probability).toFixed(0)}%
          <small title="Вероятность по линии, маржа снята">линия ${Number(p.line_probability).toFixed(0)}%</small></div>
      </div>`).join('') : `<div class="adm-muted">${empty}</div>`;
  }

  picksDraftChanged() {
    const { draft, markets, oddsMin, oddsMax } = this.picks;
    return draft.markets.slice().sort().join(',') !== markets.slice().sort().join(',')
      || String(draft.oddsMin) !== String(oddsMin) || String(draft.oddsMax) !== String(oddsMax);
  }

  syncPicksApply() {
    const btn = this.root.querySelector('[data-adm-picks-apply]');
    if (btn) btn.disabled = !this.picksDraftChanged();
  }

  applyPicksFilters() {
    const { draft } = this.picks;
    const lo = draft.oddsMin === '' ? null : Number(draft.oddsMin);
    const hi = draft.oddsMax === '' ? null : Number(draft.oddsMax);
    if ((lo !== null && !(lo >= 1)) || (hi !== null && !(hi >= 1))) {
      this.toast('Кэф — число от 1', true);
      return;
    }
    if (lo !== null && hi !== null && lo > hi) {
      this.toast('«От» больше, чем «до»', true);
      return;
    }
    Object.assign(this.picks, { markets: [...draft.markets], oddsMin: draft.oddsMin, oddsMax: draft.oddsMax });
    this.loadPicks(false).catch(err => this.toast(err.message, true));
  }

  // ─── Лимиты: вся лига, дивизионы, личные ───────────────────────────────

  async loadLimits() {
    const limits = await this.get(`${PANEL}/limits`);
    await this.refreshMe();
    if (this.tab !== 'limits') return;
    this._limitsMeta = { defaults: limits.defaults || {}, bounds: limits.bounds || {} };

    const me = this.me;
    const canEdit = limits.can_edit;
    const globalKeys = new Set((me.limit_keys && me.limit_keys.global) || []);
    const divisionKeys = (me.limit_keys && me.limit_keys.division) || [];
    const defaults = limits.defaults || {};
    const divisions = limits.divisions.filter(d => !this.divisionId || d.id === this.divisionId);
    const showGlobal = me.is_global && !this.divisionId;

    const globalCards = showGlobal ? LIMIT_GROUPS.map(g => {
      const keys = g.keys.filter(k => globalKeys.has(k));
      if (!keys.length) return '';
      return `
        <div class="adm-card">
          <div class="adm-card-title">${esc(g.title)}</div>
          ${keys.map(k => this.limitRow('global', 0, k, limits.global_overrides[k], limits.system[k], 'вся лига', canEdit,
            `${LIMIT_HINTS[k] || ''} · по умолчанию ${limitValue(k, defaults[k])}`)).join('')}
        </div>`;
    }).join('') : '';

    const divisionCards = divisions.map(d => `
      <div class="adm-card">
        <div class="adm-card-title">${esc(d.name)}</div>
        <div class="adm-muted adm-mb">Строже лиги, если задано. Без своего значения действует лимит лиги.</div>
        ${divisionKeys.map(k => this.limitRow('division', d.id, k, d.overrides[k], d.effective[k], d.name, canEdit,
          `${LIMIT_HINTS[k] || ''} · лига: ${limitValue(k, limits.system[k])}`)).join('')}
      </div>`).join('');

    const users = limits.user_overrides || [];
    const personal = me.is_global ? `
      <div class="adm-card">
        <div class="adm-card-title">Личные лимиты игроков</div>
        <div class="adm-muted adm-mb">Задаются в карточке игрока на вкладке «Игроки».</div>
        ${users.length ? users.map(u => `
          <button class="adm-row" data-adm-player="${u.user_id}">
            <div class="adm-row-main">${esc(playerName(u))}
              <small>${Object.entries(u.limits).map(([k, v]) => `${esc(LIMIT_LABELS[k] || k)}: ${limitValue(k, v)}`).join(' · ')}</small></div>
            <div class="adm-row-side gold">${Object.keys(u.limits).length} ✎</div>
          </button>`).join('') : '<div class="adm-muted">Ни у кого нет личных лимитов</div>'}
      </div>` : '';

    this.body().innerHTML = `
      ${canEdit
        ? '<div class="adm-muted adm-mb">Нажмите на строку, чтобы изменить значение. ✎ — значение задано вручную.</div>'
        : '<div class="adm-banner">Менять лимиты может только главный админ.</div>'}
      ${globalCards}
      ${divisionCards}
      ${personal}
    `;
  }

  // ─── Риски: остановка приёма, алерты, журнал ──────────────────────────

  async loadRisk() {
    const alertParams = { status: 'active', limit: 30 };
    if (this.divisionId) alertParams.division_id = this.divisionId;
    const auditParams = { limit: 30 };
    if (this.divisionId) auditParams.division_id = this.divisionId;

    const [alerts, audit] = await Promise.all([
      this.get('/api/admin/risk/alerts', alertParams).catch(() => ({ alerts: [] })),
      this.get('/api/admin/audit-log', auditParams).catch(() => ({ audit_log: [] })),
    ]);
    await this.refreshMe();
    if (this.tab !== 'risk') return;

    const me = this.me;
    const pause = me.pause || {};

    const pauseRows = [];
    if (me.is_global && !this.divisionId) {
      pauseRows.push(this.pauseRow(null, 'Вся лига', pause.global));
    }
    me.divisions
      .filter(d => !this.divisionId || d.id === this.divisionId)
      .forEach(d => pauseRows.push(this.pauseRow(d.id, d.name, (pause.divisions || {})[String(d.id)])));

    // Алерт без дивизиона — это дивизион 1, как и на сервере: чужие алерты
    // админ дивизиона видит, но принять или закрыть их не может.
    const ownDivs = new Set(me.divisions.map(d => d.id));
    const canHandleAlert = a => me.is_global || ownDivs.has(a.division_id ?? 1);

    this.body().innerHTML = `
      <div class="adm-card">
        <div class="adm-card-title">Экстренная остановка приёма</div>
        <div class="adm-muted adm-mb">Останавливает новые ставки, рынки и принятые купоны не трогает.</div>
        ${pauseRows.join('')}
      </div>

      <div class="adm-card">
        <div class="adm-card-title">Риск-алерты</div>
        ${(alerts.alerts || []).length ? alerts.alerts.map(a => `
          <div class="adm-alert adm-sev-${esc(a.severity)}">
            <div class="adm-alert-msg">${esc(a.message)}</div>
            <small class="adm-muted">${esc(a.severity)} · ${esc(shortTime(a.created_at))}${a.match_id ? ` · матч #${esc(a.match_id)}` : ''}</small>
            ${canHandleAlert(a) ? `<div class="adm-actions">
              <button class="adm-btn small" data-adm-alert="ack" data-alert-id="${a.id}">Принять</button>
              <button class="adm-btn small" data-adm-alert="resolve" data-alert-id="${a.id}">Решено</button>
            </div>` : ''}
          </div>`).join('') : '<div class="adm-muted">Активных алертов нет</div>'}
      </div>

      <div class="adm-card">
        <div class="adm-card-title">Журнал действий</div>
        ${(audit.audit_log || []).length ? audit.audit_log.map(r => `
          <div class="adm-tx">
            <div><b>${esc(AUDIT_LABELS[r.action] || r.action)}</b>
              <small class="adm-muted">${esc(shortTime(r.created_at))} · ${esc(r.entity_type)} #${esc(r.entity_id)} · админ ${esc(r.actor_id)}</small></div>
          </div>`).join('') : '<div class="adm-muted">Записей нет</div>'}
      </div>
    `;
  }

  pauseRow(divisionId, name, entry) {
    return `
      <div class="adm-pause-row">
        <div class="adm-row-main">
          <b>${esc(name)}</b>
          <small class="${entry ? 'red' : 'green'}">${entry
            ? `остановлен ${esc(shortTime(entry.at))}${entry.reason ? ` — ${esc(entry.reason)}` : ''}`
            : 'приём идёт'}</small>
        </div>
        <button class="adm-btn small ${entry ? '' : 'danger'}" data-adm-pause="${entry ? 'resume' : 'pause'}"
                data-division-id="${divisionId ?? ''}" data-name="${esc(name)}">${entry ? 'Возобновить' : 'Остановить'}</button>
      </div>`;
  }

  limitRow(scopeType, scopeId, key, override, effective, owner, canEdit, hint = '') {
    return `
      <button class="adm-row adm-limit-row" ${canEdit ? '' : 'disabled'} data-adm-limit="${scopeType}" data-scope-id="${scopeId}"
              data-key="${key}" data-current="${override ?? ''}" data-effective="${effective ?? ''}" data-owner="${esc(owner)}">
        <div class="adm-row-main">${esc(LIMIT_LABELS[key] || key)}${hint ? `<small>${hint}</small>` : ''}</div>
        <div class="adm-row-side ${override != null ? 'gold' : ''}">${limitValue(key, effective)}${override != null ? ' ✎' : ''}</div>
      </button>`;
  }

  askPause(action, divisionId, name) {
    const division = divisionId === '' ? null : Number(divisionId);
    if (action === 'resume') {
      this.openForm({
        title: 'Возобновить приём ставок?',
        desc: name,
        fields: [],
        submitLabel: 'Возобновить',
        onSubmit: async () => {
          await this.post(`${PANEL}/pause`, { paused: false, division_id: division });
          this.toast('Приём ставок возобновлён');
          await this.loadRisk();
        },
      });
      return;
    }
    this.openForm({
      title: 'Остановить приём ставок?',
      desc: `${name}: новые купоны перестанут приниматься, пока вы не возобновите приём.`,
      fields: [{ name: 'reason', label: 'Причина (обязательно)', type: 'text' }],
      submitLabel: 'Остановить',
      danger: true,
      onSubmit: async ({ reason }) => {
        if (!reason) throw new Error('Укажите причину.');
        await this.post(`${PANEL}/pause`, { paused: true, division_id: division, reason });
        this.toast('Приём ставок остановлен');
        await this.loadRisk();
      },
    });
  }

  async askLimit(scopeType, scopeId, key, current, effective, owner) {
    if (!this._limitsMeta) {
      // Карточка игрока открыта раньше вкладки «Лимиты»: границы и умолчания берём с сервера.
      try {
        const l = await this.get(`${PANEL}/limits`);
        this._limitsMeta = { defaults: l.defaults || {}, bounds: l.bounds || {} };
      } catch (e) { /* без подсказок: сервер всё равно проверит значение */ }
    }
    const meta = this._limitsMeta || {};
    const [low, high] = (meta.bounds || {})[key] || [1, 100000000];
    const def = (meta.defaults || {})[key];
    // Сброс глобального — к значению по умолчанию, дивизиона и игрока — к уровню выше.
    const resetTo = scopeType === 'global'
      ? `значение по умолчанию${def != null ? ` (${limitValue(key, def)})` : ''}`
      : scopeType === 'division' ? 'лимит лиги' : 'лимит дивизиона или лиги';
    this.openForm({
      title: LIMIT_LABELS[key] || key,
      desc: `${owner} · сейчас ${limitValue(key, effective)}${current !== '' ? ' (задано вручную)' : ''}.`
        + `${LIMIT_HINTS[key] ? ` ${LIMIT_HINTS[key]}.` : ''} Допустимо ${fmt(low)}–${fmt(high)}. Пустое поле — вернуть ${resetTo}.`,
      fields: [{ name: 'value', label: 'Новое значение', type: 'number', step: '1', min: String(low), value: current }],
      submitLabel: 'Сохранить',
      onSubmit: async ({ value }) => {
        const trimmed = String(value).trim();
        const parsed = trimmed === '' ? null : parseInt(trimmed, 10);
        if (parsed !== null && !(parsed >= low && parsed <= high)) {
          throw new Error(`Значение — целое число от ${fmt(low)} до ${fmt(high)}.`);
        }
        await this.post(`${PANEL}/limits`, { scope_type: scopeType, scope_id: Number(scopeId), limit_key: key, value: parsed });
        this.toast(parsed === null ? 'Значение сброшено' : 'Значение сохранено');
        if (scopeType === 'user') {
          await this.openPlayer(Number(scopeId));
          if (this.tab === 'limits') this.loadLimits().catch(() => {});
        } else {
          await this.loadLimits();
        }
      },
    });
  }

  async alertAction(kind, alertId) {
    try {
      await this.post(`/api/admin/risk/alerts/${alertId}/${kind}`, {});
      this.toast(kind === 'ack' ? 'Алерт принят' : 'Алерт закрыт');
      await this.loadRisk();
    } catch (e) {
      this.toast(e.message, true);
    }
  }

  // ─── Модалка и формы ───────────────────────────────────────────────────

  modalFrame(title, body) {
    return `
      <div class="adm-modal-head">
        <div class="adm-modal-title">${esc(title)}</div>
        <button class="btn-modal-close btn-modal-x" data-adm-close>✕</button>
      </div>
      <div class="adm-modal-body">${body}</div>`;
  }

  showModal(html) {
    this._modalSubmit = null;
    this.modal.querySelector('.adm-modal').innerHTML = html;
    this.modal.classList.add('active');
  }

  closeModal() {
    this._modalSubmit = null;
    this.modal.classList.remove('active');
  }

  openForm({ title, desc, fields, submitLabel, danger = false, onSubmit }) {
    const inputs = fields.map(f => `
      <label class="adm-field">
        <span>${esc(f.label)}</span>
        <input class="adm-input" name="${f.name}" type="${f.type}" ${f.step ? `step="${f.step}"` : ''}
               ${f.min ? `min="${f.min}"` : ''} ${f.type === 'number' ? 'inputmode="decimal"' : ''}
               value="${esc(f.value ?? '')}" maxlength="300" autocomplete="off">
      </label>`).join('');
    this.showModal(this.modalFrame(title, `
      <form class="adm-form" data-adm-form>
        ${desc ? `<div class="adm-form-desc">${esc(desc)}</div>` : ''}
        ${inputs}
        <div class="adm-form-error"></div>
        <button type="submit" class="adm-btn wide ${danger ? 'danger' : 'primary'}">${esc(submitLabel)}</button>
      </form>`));
    this._modalSubmit = onSubmit;
    const first = this.modal.querySelector('.adm-form input');
    if (first) setTimeout(() => first.focus(), 50);
  }

  async submitForm(form) {
    if (this.busy || !this._modalSubmit) return;
    const values = {};
    form.querySelectorAll('input').forEach(i => { values[i.name] = i.value.trim(); });
    const errorEl = form.querySelector('.adm-form-error');
    const button = form.querySelector('button[type="submit"]');
    const onSubmit = this._modalSubmit;
    this.busy = true;
    button.disabled = true;
    errorEl.textContent = '';
    try {
      // Успешный обработчик сам решает, что показать дальше (карточку игрока и т. п.),
      // поэтому модалку закрываем до него, а не после.
      this.closeModal();
      await onSubmit(values);
      tgBridge.hapticNotification('success');
    } catch (e) {
      // Ошибка проверки или ответа сервера — возвращаем форму с сообщением.
      this.modal.classList.add('active');
      this._modalSubmit = onSubmit;
      errorEl.textContent = e.message || 'Не получилось';
      button.disabled = false;
      tgBridge.hapticNotification('error');
    } finally {
      this.busy = false;
    }
  }

  toast(message, isError = false) {
    let el = document.getElementById('adm-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'adm-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.className = `adm-toast show ${isError ? 'error' : ''}`;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { el.className = 'adm-toast'; }, 2600);
  }

  // ─── События ───────────────────────────────────────────────────────────

  bind() {
    this.root.addEventListener('click', (e) => this.onClick(e));
    this.modal.addEventListener('click', (e) => {
      if (e.target === this.modal || e.target.closest('[data-adm-close]')) {
        this.closeModal();
        return;
      }
      this.onClick(e);
    });
    this.modal.addEventListener('submit', (e) => {
      const form = e.target.closest('[data-adm-form]');
      if (!form) return;
      e.preventDefault();
      this.submitForm(form);
    });
    this.root.addEventListener('input', (e) => {
      if (e.target.id === 'adm-market-search') {
        this.debounce(() => { this.markets.q = e.target.value.trim(); this.reloadList('markets'); });
      } else if (e.target.id === 'adm-player-search') {
        this.debounce(() => { this.players.q = e.target.value.trim(); this.reloadList('players'); });
      } else if (e.target.id === 'adm-picks-odds-min' || e.target.id === 'adm-picks-odds-max') {
        const key = e.target.id === 'adm-picks-odds-min' ? 'oddsMin' : 'oddsMax';
        this.picks.draft[key] = e.target.value.trim().replace(',', '.');
        this.syncPicksApply();
      }
    });
  }

  debounce(fn) {
    clearTimeout(this._searchTimer);
    this._searchTimer = setTimeout(fn, 350);
  }

  reloadList(tab) {
    const run = tab === 'markets' ? this.loadMarketsList() : this.loadPlayers();
    run.catch(e => this.toast(e.message, true));
  }

  async loadMarketsList() {
    // Поиск перерисовывает только список, чтобы поле ввода не теряло фокус.
    this.markets.offset = 0;
    this.markets.items = [];
    await this.loadMarkets(false);
  }

  onClick(e) {
    const t = (sel) => e.target.closest(sel);
    let el;

    if ((el = t('[data-adm-tab]'))) {
      this.tab = el.dataset.admTab;
      this.root.querySelectorAll('[data-adm-tab]').forEach(b => b.classList.toggle('active', b === el));
      tgBridge.hapticImpact('light');
      this.loadTab();
    } else if ((el = t('[data-adm-division]'))) {
      this.divisionId = el.dataset.admDivision ? Number(el.dataset.admDivision) : null;
      this.root.querySelectorAll('[data-adm-division]').forEach(b => b.classList.toggle('active', b === el));
      this.loadTab();
    } else if (t('[data-adm-refresh]')) {
      api.cache.clear();
      this.refreshMe();
      this.loadTab();
    } else if (t('[data-adm-picks-refresh]')) {
      this.loadPicks(true).catch(err => this.toast(err.message, true));
    } else if ((el = t('[data-adm-pmarket]'))) {
      const draft = this.picks.draft;
      const id = el.dataset.admPmarket;
      if (!id) draft.markets = [];
      else if (draft.markets.includes(id)) draft.markets = draft.markets.filter(x => x !== id);
      else draft.markets = [...draft.markets, id];
      this.root.querySelectorAll('[data-adm-pmarket]').forEach(b => {
        const bid = b.dataset.admPmarket;
        b.classList.toggle('active', bid ? draft.markets.includes(bid) : !draft.markets.length);
      });
      this.syncPicksApply();
    } else if (t('[data-adm-picks-apply]')) {
      this.applyPicksFilters();
    } else if ((el = t('[data-adm-pchance]'))) {
      this.picks.minChance = Number(el.dataset.admPchance);
      this.root.querySelectorAll('[data-adm-pchance]').forEach(b => b.classList.toggle('active', b === el));
      this.renderPicksList();
    } else if ((el = t('[data-adm-pvalue]'))) {
      this.picks.valueOnly = !this.picks.valueOnly;
      el.classList.toggle('active', this.picks.valueOnly);
      this.renderPicksList();
    } else if ((el = t('[data-adm-mstate]'))) {
      this.markets.state = el.dataset.admMstate;
      this.loadTab();
    } else if ((el = t('[data-adm-bstatus]'))) {
      this.bets.status = el.dataset.admBstatus;
      this.loadTab();
    } else if (t('[data-adm-clear-user]')) {
      this.bets.userId = null;
      this.loadTab();
    } else if ((el = t('[data-adm-psort]'))) {
      this.players.sort = el.dataset.admPsort;
      this.root.querySelectorAll('[data-adm-psort]').forEach(b => b.classList.toggle('active', b === el));
      this.reloadList('players');
    } else if ((el = t('[data-adm-pbanned]'))) {
      this.players.banned = !this.players.banned;
      el.classList.toggle('active', this.players.banned);
      this.reloadList('players');
    } else if ((el = t('[data-adm-more]'))) {
      el.disabled = true;
      const run = el.dataset.admMore === 'markets' ? this.loadMarkets(false) : this.loadBets(false);
      run.catch(err => { el.disabled = false; this.toast(err.message, true); });
    } else if ((el = t('[data-adm-market-action]'))) {
      this.askMarketAction(el.dataset.admMarketAction, el.dataset.marketId, el.dataset.marketName);
    } else if ((el = t('[data-adm-selection]'))) {
      if (el.disabled) return;
      this.askOdds(el.dataset.admSelection, el.dataset.name, el.dataset.odds, el.dataset.model);
    } else if ((el = t('[data-adm-void-bet]'))) {
      this.askVoidBet(el.dataset.admVoidBet, el.dataset.amount);
    } else if ((el = t('[data-adm-bet]'))) {
      this.openBet(el.dataset.admBet);
    } else if ((el = t('[data-adm-player-bets]'))) {
      this.closeModal();
      this.bets.userId = Number(el.dataset.admPlayerBets);
      this.bets.status = 'all';
      this.tab = 'bets';
      this.renderShell();
      this.loadTab();
    } else if ((el = t('[data-adm-adjust]'))) {
      this.askAdjust(el.dataset.admAdjust, Number(el.dataset.sign));
    } else if ((el = t('[data-adm-ban]'))) {
      this.askBan(el.dataset.admBan);
    } else if ((el = t('[data-adm-unban]'))) {
      this.unban(el.dataset.admUnban);
    } else if ((el = t('[data-adm-player]'))) {
      if (el.disabled || !this.me.is_global) return;
      this.openPlayer(el.dataset.admPlayer);
    } else if ((el = t('[data-adm-pause]'))) {
      this.askPause(el.dataset.admPause, el.dataset.divisionId, el.dataset.name);
    } else if ((el = t('[data-adm-limit]'))) {
      if (el.disabled) return;
      const d = el.dataset;
      this.askLimit(d.admLimit, d.scopeId, d.key, d.current, d.effective, d.owner);
    } else if ((el = t('[data-adm-alert]'))) {
      this.alertAction(el.dataset.admAlert, el.dataset.alertId);
    }
  }
}
