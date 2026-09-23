/**
 * web/js/store.js
 * Centralized Reactive State Store for Logovo.bet (v2.0).
 */

import { tgBridge } from './tg.js';

class StateStore {
  constructor() {
    this.state = {
      user: null,
      tours: [],
      marketCategoryFilter: 'all',
      searchQuery: '',
      slip: [], // [ { match_id, outcome, odd, market_id, selection_id, selection_name, market_name, team1_name, team2_name, tour }, ... ]
      slipMode: 'express', // preferred mode for 2+ events: 'express' | 'single'
      stakeAmount: 100,
      // Per-event stakes for batch singles, keyed by match_id. A missing key
      // falls back to stakeAmount; kept off the slip items so they never leak
      // into the API payload or saved drafts.
      singleStakes: {},
      activeView: 'lobby', // 'lobby' | 'match_center' | 'tournaments' | 'history' | 'my_club' | 'profile'
      selectedMatchId: null,
      matchCenterSubTab: 'markets', // 'markets' | 'stats' | 'insights'
      matchDetail: null,
      matchStats: null,
      matchH2H: null,
      matchInsights: null,
      matchLive: null,
      matchMarkets: [],
      standings: [],
      standingsForm: {},
      results: [],
      // Лидеры дивизиона: бомбардиры, ассистенты и обладатели награды «Игрок матча».
      // Ключи повторяют ответ /api/tournaments/{id}/top-scorers, чтобы не плодить
      // переименования между API и рендером.
      tournamentTopStats: { top_scorers: [], top_assists: [], top_mvps: [] },
      myBets: [],
      myBetsFilter: 'all',
      savedCoupons: [],
      favorites: [],
      notifications: [],
      myStats: null,
      leaderboard: [],
      myRank: null,
      progression: { level: 1, current_xp: 0, total_xp_earned: 0, equipped_title: 'Новичок' },
      streak: { streak: 1, best_streak: 1, streak_shield_count: 1 },
      achievements: [],
      profile: null,
      divisions: [],
      selectedDivisionId: 1,
      unclaimedAchievementsCount: 0,
      // Вкладка «Мой Клуб» (личный кабинет игрока)
      myClub: { overview: null, matches: [], squad: [] },
      myClubRecent: [],
      myClubSquadMeta: { top_scorer: null, top_assistant: null, top_mvp: null },
      myClubSubTab: 'matches', // 'matches' | 'squad' | 'history'
      myClubLoading: false,
      // Sports Intelligence State (LIVE-центр удалён)
      oddsMovers: [],
      hotMatches: [],
      recommendations: [],
      capperLeaderboard: [],
      // Лобби показывает либо линию дивизиона, либо общий кубок.
      lobbyMode: 'league', // 'league' | 'cup'
      cup: {
        stages: [],
        selectedStageId: null,
        view: 'line', // 'line' | 'bracket'
        line: null,
        bracket: null,
        loading: false,
        error: null
      }
    };
    this.listeners = new Set();
    this._notifyQueued = false;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * Несколько set*() подряд (например, Promise.all с пятью ответами) дают один
   * проход подписчиков в конце текущей задачи, а не пять перерисовок подряд.
   */
  notify() {
    if (this._notifyQueued) return;
    this._notifyQueued = true;
    queueMicrotask(() => this._flush());
  }

  _flush() {
    this._notifyQueued = false;
    for (const listener of this.listeners) {
      try {
        listener(this.state);
      } catch (e) {
        console.error("Store listener error:", e);
      }
    }
  }

  setUser(user) {
    this.state.user = user;
    this.notify();
  }

  setProgression(progression, streak, unclaimedAch) {
    if (progression) this.state.progression = progression;
    if (streak) this.state.streak = streak;
    this.state.unclaimedAchievementsCount = unclaimedAch || 0;
    this.notify();
  }

  setAchievements(achievements) {
    this.state.achievements = achievements || [];
    this.notify();
  }

  setProfile(profile) {
    this.state.profile = profile;
    this.notify();
  }

  setTours(tours) {
    this.state.tours = tours || [];
    this.notify();
  }

  setDivisions(divisions) {
    this.state.divisions = divisions || [];
    this.notify();
  }

  setSelectedDivisionId(divisionId) {
    this.state.selectedDivisionId = divisionId ? parseInt(divisionId) : 1;
    this.notify();
  }

  setMarketCategoryFilter(category) {
    this.state.marketCategoryFilter = category;
    this.notify();
  }

  setSearchQuery(query) {
    this.state.searchQuery = query || '';
    this.notify();
  }

  setActiveView(viewName) {
    this.state.activeView = viewName;
    this.notify();
  }

  setSelectedMatch(matchId, detail, stats, h2h, insights, live, markets) {
    this.state.selectedMatchId = matchId;
    if (detail) this.state.matchDetail = detail;
    if (stats) this.state.matchStats = stats;
    if (h2h) this.state.matchH2H = h2h;
    if (insights) this.state.matchInsights = insights;
    if (live) this.state.matchLive = live;
    if (markets) this.state.matchMarkets = markets;
    this.state.matchCenterSubTab = 'markets';
    this.notify();
  }

  setMatchCenterSubTab(tab) {
    this.state.matchCenterSubTab = tab || 'markets';
    this.notify();
  }

  setTournamentData(standings, results, topStats, form = null) {
    if (standings) this.state.standings = standings;
    if (results) this.state.results = results;
    if (topStats) {
      this.state.tournamentTopStats = {
        top_scorers: topStats.top_scorers || [],
        top_assists: topStats.top_assists || [],
        top_mvps: topStats.top_mvps || []
      };
    }
    // Форма последних матчей приходит вместе с таблицей; пустой ответ её не стирает.
    if (form) this.state.standingsForm = form;
    this.notify();
  }

  setMyBets(bets, filter = null) {
    this.state.myBets = bets || [];
    if (filter) this.state.myBetsFilter = filter;
    this.notify();
  }

  setSavedCoupons(saved) {
    this.state.savedCoupons = saved || [];
    this.notify();
  }

  setFavorites(favorites) {
    this.state.favorites = favorites || [];
    this.notify();
  }

  setNotifications(notifications) {
    this.state.notifications = notifications || [];
    this.notify();
  }

  setMyStats(stats) {
    this.state.myStats = stats;
    this.notify();
  }

  setLeaderboard(leaderboard, myRank) {
    this.state.leaderboard = leaderboard || [];
    this.state.myRank = myRank;
    this.notify();
  }

  // --- My Club («Мой Клуб») ---
  setMyClubOverview(overview) {
    this.state.myClub.overview = overview || null;
    this.notify();
  }

  setMyClubMatches(matches, recent = null) {
    this.state.myClub.matches = matches || [];
    if (recent) this.state.myClubRecent = recent;
    this.notify();
  }

  setMyClubSquad(players, topScorer = null, topAssistant = null, topMvp = null) {
    this.state.myClub.squad = players || [];
    this.state.myClubSquadMeta = { top_scorer: topScorer, top_assistant: topAssistant, top_mvp: topMvp };
    this.notify();
  }

  setMyClubSubTab(tab) {
    this.state.myClubSubTab = tab || 'matches';
    this.notify();
  }

  setMyClubLoading(isLoading) {
    this.state.myClubLoading = !!isLoading;
    this.notify();
  }

  // --- Smart Bet Slip Operations ---
  _buildSlipItem(match, outcome, odd, extra) {
    return {
      match_id: match.match_id || match.id,
      outcome,
      odd: parseFloat(odd),
      market_id: extra.market_id || null,
      selection_id: extra.selection_id || null,
      selection_name: extra.selection_name || outcome.toUpperCase(),
      market_name: extra.market_name || null,
      team1_name: match.team1_name || match.player1_team || 'Хозяева',
      team2_name: match.team2_name || match.player2_team || 'Гости',
      tour: match.tour || match.round_number || 1,
      // Подпись события вместо «Тур N» — у кубка туров нет.
      ...(extra.meta ? { meta: extra.meta } : {})
    };
  }

  // --- Общий кубок ---
  setLobbyMode(mode) {
    this.state.lobbyMode = mode === 'cup' ? 'cup' : 'league';
    this.notify();
  }

  setCupState(patch) {
    this.state.cup = { ...this.state.cup, ...patch };
    this.notify();
  }

  /** Тайл кубковой линии по match_id — с именами пары из серии. */
  findCupTile(matchId) {
    const series = this.state.cup.line?.series || [];
    for (const entry of series) {
      for (const tile of [entry.header, ...(entry.games || [])]) {
        if (tile && tile.match_id === matchId) {
          return {
            ...tile,
            team1_name: tile.team1_name || entry.team1_name,
            team2_name: tile.team2_name || entry.team2_name
          };
        }
      }
    }
    return null;
  }

  toggleSelection(match, outcome, odd, extra = {}) {
    const mId = match.match_id || match.id;
    const existingIndex = this.state.slip.findIndex(s => s.match_id === mId);

    if (existingIndex >= 0) {
      const current = this.state.slip[existingIndex];
      if (current.outcome === outcome) {
        // Deselect
        this.state.slip.splice(existingIndex, 1);
        delete this.state.singleStakes[mId];
        tgBridge.hapticImpact('light');
      } else {
        // Switch pick in same match
        this.state.slip[existingIndex] = this._buildSlipItem(match, outcome, odd, extra);
        tgBridge.hapticImpact('medium');
      }
    } else {
      this.state.slip.push(this._buildSlipItem(match, outcome, odd, extra));
      tgBridge.hapticImpact('medium');
    }
    this.notify();
  }

  loadCouponSelections(selections) {
    this.state.slip = selections || [];
    this.state.singleStakes = {};
    tgBridge.hapticNotification('success');
    this.notify();
  }

  removeSelection(matchId) {
    this.state.slip = this.state.slip.filter(s => s.match_id !== matchId);
    delete this.state.singleStakes[matchId];
    tgBridge.hapticImpact('light');
    this.notify();
  }

  clearSlip() {
    this.state.slip = [];
    this.state.singleStakes = {};
    tgBridge.hapticImpact('light');
    this.notify();
  }

  setSlipMode(mode) {
    this.state.slipMode = mode === 'single' ? 'single' : 'express';
    this.notify();
  }

  /**
   * The main stake. In batch-singles mode it is the "per event" amount, so
   * changing it resets every individual override back to it.
   */
  setStakeAmount(amount) {
    this.state.stakeAmount = Math.max(0, parseInt(amount) || 0);
    this.state.singleStakes = {};
    this.notify();
  }

  setSingleStake(matchId, amount) {
    this.state.singleStakes[matchId] = Math.max(0, parseInt(amount) || 0);
    this.notify();
  }

  // --- Derived Calculations ---

  /** One event is always a single; 2+ follow the user's preferred mode. */
  getSlipMode() {
    return this.state.slip.length < 2 ? 'single' : this.state.slipMode;
  }

  isBatchSingles() {
    return this.state.slip.length > 1 && this.state.slipMode === 'single';
  }

  getSingleStake(matchId) {
    const own = this.state.singleStakes[matchId];
    return own === undefined ? this.state.stakeAmount : own;
  }

  /**
   * The amount the main stake field stands for: the express/single stake, or in
   * batch singles the one stake every event shares — null once they differ.
   */
  getCommonStake() {
    if (!this.isBatchSingles()) return this.state.stakeAmount;
    const stakes = new Set(this.state.slip.map(s => this.getSingleStake(s.match_id)));
    return stakes.size === 1 ? [...stakes][0] : null;
  }

  /** "+N" chips: with differing single stakes each event's own stake grows. */
  addToStake(delta) {
    const common = this.getCommonStake();
    if (common !== null) {
      this.setStakeAmount(common + delta);
      return;
    }
    this.state.slip.forEach(s => {
      this.state.singleStakes[s.match_id] = this.getSingleStake(s.match_id) + delta;
    });
    this.notify();
  }

  getTotalOdd() {
    if (this.state.slip.length === 0) return 1.0;
    const rawOdd = this.state.slip.reduce((acc, item) => acc * item.odd, 1.0);
    return Math.round(rawOdd * 100) / 100;
  }

  getTotalStake() {
    if (this.isBatchSingles()) {
      return this.state.slip.reduce((sum, item) => sum + this.getSingleStake(item.match_id), 0);
    }
    return this.state.stakeAmount;
  }

  getPotentialWin() {
    if (this.state.slip.length === 0) return 0;
    if (this.isBatchSingles()) {
      return this.state.slip.reduce(
        (sum, item) => sum + Math.round(this.getSingleStake(item.match_id) * item.odd), 0);
    }
    // Same rounding as the server (database.place_user_bet, settlement_engine).
    return Math.round(this.state.stakeAmount * this.getTotalOdd());
  }

  getBetLimits() {
    const l = this.state.user?.bet_limits || {};
    return {
      min_bet: l.min_bet || 10,
      max_bet: l.max_bet || 50000,
      max_payout: l.max_payout || 10000,
      max_open_exposure: l.max_open_exposure || 35000,
      open_exposure: l.open_exposure || 0,
      max_open_bets: l.max_open_bets || 12,
      open_bets: l.open_bets || 0
    };
  }

  /** Potential win still available under the open-bets limit. */
  getRemainingExposure() {
    const { max_open_exposure, open_exposure } = this.getBetLimits();
    return Math.max(0, max_open_exposure - open_exposure);
  }

  /** How many more coupons the player may keep open at once. */
  getRemainingBetSlots() {
    const { max_open_bets, open_bets } = this.getBetLimits();
    return Math.max(0, max_open_bets - open_bets);
  }

  /**
   * A freshly placed bet takes its potential win out of the open-bets limit.
   * Bootstrap refreshes the exact figure; this keeps the coupon honest until then.
   */
  addOpenExposure(win) {
    const user = this.state.user;
    if (!user) return;
    const l = user.bet_limits || {};
    this.setUser({ ...user, bet_limits: { ...l, open_exposure: (l.open_exposure || 0) + Math.max(0, win || 0) } });
  }

  /**
   * One placed coupon takes one slot — an express of five matches counts once,
   * exactly as the server counts it. Bootstrap refreshes the exact figure.
   */
  addOpenBet(count = 1) {
    const user = this.state.user;
    if (!user) return;
    const l = user.bet_limits || {};
    this.setUser({ ...user, bet_limits: { ...l, open_bets: (l.open_bets || 0) + Math.max(0, count) } });
  }

  /** Resync the slot counter from a server rejection, without waiting for bootstrap. */
  setOpenBets(openBets, maxOpenBets) {
    const user = this.state.user;
    if (!user) return;
    const l = user.bet_limits || {};
    const next = { ...l };
    if (Number.isFinite(openBets)) next.open_bets = openBets;
    if (Number.isFinite(maxOpenBets)) next.max_open_bets = maxOpenBets;
    this.setUser({ ...user, bet_limits: next });
  }

  /**
   * Largest stake the payout cap alone allows at the coupon's odds
   * (the tightest single for a batch of singles).
   */
  getMaxStakeByPayout() {
    const slip = this.state.slip;
    const { max_payout } = this.getBetLimits();
    const odds = this.isBatchSingles() ? slip.map(s => s.odd) : [this.getTotalOdd()];
    return Math.min(...odds.map(o => Math.floor(max_payout / Math.max(1, o || 1))));
  }

  /**
   * Largest per-bet stake the rules allow right now: capped by the balance
   * (split across the batch for singles), the max bet, the max payout
   * at the coupon's odds and what is left of the open-bets limit.
   * The server re-checks all of it on placement.
   */
  getMaxStake() {
    const slip = this.state.slip;
    const { max_bet } = this.getBetLimits();
    const balance = Math.max(0, Math.floor(this.state.user?.balance || 0));
    const bets = this.isBatchSingles() ? slip.length : 1;
    const odds = this.isBatchSingles() ? slip.map(s => s.odd) : [this.getTotalOdd()];
    const remaining = this.getRemainingExposure();
    const byExposure = Math.min(...odds.map(o => Math.floor(remaining / bets / Math.max(1, o || 1))));
    return Math.max(0, Math.min(Math.floor(balance / bets), max_bet, this.getMaxStakeByPayout(), byExposure));
  }

  isSelectionActive(matchId, outcome) {
    return this.state.slip.some(s => s.match_id === matchId && s.outcome === outcome);
  }

  // --- Intelligence Setters ---
  setOddsMovers(movers) {
    this.state.oddsMovers = movers || [];
    this.notify();
  }

  setHotMatches(matches) {
    this.state.hotMatches = matches || [];
    this.notify();
  }

  setRecommendations(recs) {
    this.state.recommendations = recs || [];
    this.notify();
  }

  setCapperLeaderboard(leaderboard) {
    this.state.capperLeaderboard = leaderboard || [];
    this.notify();
  }
}

export const store = new StateStore();
