/**
 * web/js/app.js
 * Comprehensive App Controller and Event Orchestrator for Logovo.bet (v2.0).
 */

import { api } from './api.js';
import { store } from './store.js';
import { tgBridge } from './tg.js';
import { UIRenderer, escapeHtml, cupStageLabel } from './ui.js';
import { ParticleEffects } from './effects.js';
import { AdminPanel } from './admin.js';

class AppController {
  constructor() {
    this.currentTournamentTab = 'standings';
    // Какой список лидеров открыт внутри вкладки «Лидеры»: 'scorers' | 'assists' | 'mvps'.
    this.currentLeaderTab = 'scorers';
    // Сортировка таблицы: по умолчанию как её отдаёт бэкенд — по очкам, вниз.
    this.standingsSort = { key: 'points', dir: 'desc' };
    // Подписи входных данных уже нарисованных блоков: key -> JSON.
    this._renderSigs = new Map();
    // Матч, который Матч-Центр грузит или уже показал: ответ на более старый запрос отбрасывается.
    this._matchCenterRequestedId = null;
    // Панель управления Logovo.bet создаётся при первом открытии вкладки.
    this.adminPanel = null;
    this.init();
  }

  async init() {
    // 1. Subscribe UI renderer to reactive store changes.
    // Рисуем только активный экран и только те блоки, чьи входные данные изменились:
    // иначе каждое изменение купона перестраивало innerHTML всех вкладок разом.
    store.subscribe((state) => {
      const view = state.activeView;
      const myClub = state.myClub;

      this.renderBlock('header', [state.user, state.progression, state.unclaimedAchievementsCount],
        () => UIRenderer.renderHeader(state.user, state.progression, state.unclaimedAchievementsCount));
      this.renderBlock('adminBtn', [Boolean(state.user?.is_panel_admin)], () => {
        const btn = document.getElementById('header-admin-btn');
        if (btn) btn.hidden = !state.user?.is_panel_admin;
      });
      this.renderBlock('navClubIcon', [myClub.overview],
        () => UIRenderer.updateNavClubIcon(myClub.overview));

      if (view === 'lobby') {
        this.renderBlock('lobbyDivTabs', [state.divisions, state.selectedDivisionId, state.lobbyMode],
          () => UIRenderer.renderDivisionTabs(state.divisions, state.selectedDivisionId, 'lobby-division-tabs-container', state.lobbyMode));
        this.renderBlock('hotMatches', [state.hotMatches],
          () => UIRenderer.renderHotMatches(state.hotMatches));
        this.renderBlock('oddsMovers', [state.oddsMovers],
          () => UIRenderer.renderOddsMovers(state.oddsMovers));
        this.renderBlock('recommendations', [state.recommendations, state.searchQuery],
          () => UIRenderer.renderRecommendations(state.recommendations, state.searchQuery));
        this.renderBlock('matches', [state.tours, state.marketCategoryFilter, state.searchQuery, state.selectedDivisionId],
          () => UIRenderer.renderMatches(state.tours, state.marketCategoryFilter, state.searchQuery, state.selectedDivisionId));
        this.renderBlock('lobbyMode', [state.lobbyMode],
          () => UIRenderer.renderLobbyMode(state.lobbyMode));
        if (state.lobbyMode === 'cup') {
          this.renderBlock('cup', [state.cup, state.searchQuery],
            () => UIRenderer.renderCupView(state.cup, state.searchQuery));
        }
      } else if (view === 'match_center') {
        this.renderBlock('matchCenter',
          [state.matchDetail, state.matchStats, state.matchH2H, state.matchInsights, state.matchLive, state.matchMarkets, state.matchCenterSubTab],
          () => UIRenderer.renderMatchCenter(state.matchDetail, state.matchStats, state.matchH2H, state.matchInsights, state.matchLive, state.matchMarkets, state.matchCenterSubTab));
      } else if (view === 'tournaments') {
        this.renderBlock('tournamentDivTabs', [state.divisions, state.selectedDivisionId],
          () => UIRenderer.renderDivisionTabs(state.divisions, state.selectedDivisionId, 'tournament-division-tabs-container'));
        this.renderTournamentTab();
      } else if (view === 'history') {
        this.renderBlock('history', [state.myBets, state.myBetsFilter],
          () => UIRenderer.renderPredictionsHistory(state.myBets, state.myBetsFilter));
        this.renderBlock('savedCoupons', [state.savedCoupons],
          () => UIRenderer.renderSavedCoupons(state.savedCoupons));
      } else if (view === 'profile') {
        this.renderBlock('profile', [state.user, state.progression, state.myStats, state.achievements],
          () => UIRenderer.renderProfile(state.user, state.progression, state.myStats, state.achievements));
      } else if (view === 'my_club') {
        this.renderBlock('myClubHero', [myClub.overview],
          () => UIRenderer.renderMyClubView(myClub.overview));
        if (!myClub.overview || myClub.overview.registered) {
          this.renderBlock('myClubMatches', [myClub.matches, state.myClubLoading],
            () => UIRenderer.renderMyClubMatches(myClub.matches, state.myClubLoading));
          this.renderBlock('myClubHistory', [state.myClubRecent, state.myClubLoading],
            () => UIRenderer.renderMyClubHistory(state.myClubRecent, state.myClubLoading));
          this.renderBlock('myClubSquad', [myClub.squad, state.myClubSquadMeta, state.myClubLoading],
            () => UIRenderer.renderMyClubSquad(myClub.squad, state.myClubSquadMeta, state.myClubLoading));
          this.renderBlock('myClubSubTab', [state.myClubSubTab],
            () => UIRenderer.renderMyClubSubTab(state.myClubSubTab));
        } else {
          // Онбординг очистил панели — после регистрации их нужно нарисовать заново.
          ['myClubMatches', 'myClubHistory', 'myClubSquad', 'myClubSubTab'].forEach(k => this.invalidateRender(k));
        }
      }

      // Купон перерисовывается инкрементально — сравнивать его входы дороже, чем обновить.
      UIRenderer.renderSlipDrawer(state.slip, state.stakeAmount);
      if (state.slip.length === 0 && this.isCouponOpen()) this.toggleSlipDrawer(false);

      // Подсветку выбранных исходов обновляем классом, не перерисовывая списки:
      // поэтому купон и не входит во входные данные блоков выше.
      this.renderBlock('oddSelection', [state.slip.map(s => [s.match_id, s.outcome])], () => {
        document.querySelectorAll('.odd-btn[data-match-id][data-outcome]').forEach(b => {
          b.classList.toggle('selected', store.isSelectionActive(parseInt(b.dataset.matchId), b.dataset.outcome));
        });
      });
    });

    // 2. Setup all DOM events
    this.bindEvents();

    // 3. Initial Data Load
    await this.loadInitialData();
  }

  showLockdownScreen() {
    const lockScreen = document.getElementById('app-lockdown-screen');
    if (lockScreen) lockScreen.style.display = 'flex';
    const nav = document.querySelector('.bottom-nav');
    if (nav) nav.style.display = 'none';
    ['betbar', 'coupon-sheet', 'coupon-backdrop'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
    const views = document.querySelector('.views-container');
    if (views) views.style.display = 'none';
    const header = document.querySelector('.app-header');
    if (header) header.style.display = 'none';
  }

  async loadInitialData() {
    try {
      const data = await api.getBootstrap();
      if (data.status === 'ok') {
        store.setUser(data.user);

        if (!data.user.has_access) {
          const lockScreen = document.getElementById('access-lock-screen');
          if (lockScreen) lockScreen.style.display = 'flex';
          const nav = document.querySelector('.bottom-nav');
          if (nav) nav.style.display = 'none';
          ['betbar', 'coupon-sheet', 'coupon-backdrop'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
          });
          const views = document.querySelector('.views-container');
          if (views) views.style.display = 'none';
          return;
        }

        // Parse URL query parameters (e.g. from deep links)
        const urlParams = new URLSearchParams(window.location.search);
        const targetDivId = urlParams.get('division_id');
        const targetMatchId = urlParams.get('match_id');

        // Дивизионы приходят в bootstrap; отдельный запрос — только для старого сервера.
        let divisions = Array.isArray(data.divisions) ? data.divisions : null;
        if (!divisions) {
          try {
            const divData = await api.getDivisions();
            if (divData.status === 'ok' && divData.divisions) divisions = divData.divisions;
          } catch (err) {
            console.warn("Could not load divisions:", err);
          }
        }
        if (divisions) {
          store.setDivisions(divisions);
          if (targetDivId) {
            store.setSelectedDivisionId(parseInt(targetDivId));
          } else if (divisions.length > 0 && !store.state.selectedDivisionId) {
            store.setSelectedDivisionId(divisions[0].id);
          }
        }

        const divId = store.state.selectedDivisionId;

        // Всё остальное грузится параллельно с линией, а не после неё.
        this.fetchProgressionData();
        this.fetchTournamentData(divId);
        this.fetchUserExtras();
        this.fetchIntelligenceHub();

        if (targetMatchId) {
          this.loadMatchCenter(parseInt(targetMatchId));
          this.switchView('match_center');
        }

        const toursData = await api.getTours(divId);
        if (toursData.status === 'ok') {
          store.setTours(toursData.tours);
          // Матч-Центр без выбранного матча грузится при первом открытии вкладки;
          // если её открыли раньше, чем пришла линия, — догружаем сейчас.
          if (store.state.activeView === 'match_center') this.ensureMatchCenterMatch();
        }
      }
    } catch (err) {
      if (err.status === 403 || err.code === 'LOGOVO_LOCKDOWN' || (err.data && err.data.error === 'LOGOVO_LOCKDOWN')) {
        this.showLockdownScreen();
        return;
      }
      console.error("Failed to bootstrap app:", err);
      const matchesContainer = document.getElementById('matches-list-container');
      if (matchesContainer) {
        matchesContainer.innerHTML = `
          <div class="tg-required">
            <div class="tg-required-icon">📱</div>
            <div class="tg-required-title">Откройте через Telegram</div>
            <div class="tg-required-text">
              Для работы Mini App требуется авторизация Telegram WebApp. Откройте приложение через меню бота или команду /start в Telegram.
            </div>
          </div>
        `;
      }
    }
  }

  async fetchIntelligenceHub(divId = null) {
    try {
      const dId = divId || store.state.selectedDivisionId;
      const [hotRes, moversRes, recsRes] = await Promise.all([
        api.getHotMatches(dId),
        api.getOddsMovers(),
        api.getRecommendations(dId)
      ]);
      if (hotRes.status === 'ok') store.setHotMatches(hotRes.hot_matches);
      if (moversRes.status === 'ok') store.setOddsMovers(moversRes.movers);
      if (recsRes.status === 'ok') store.setRecommendations(recsRes.recommendations);
    } catch (e) {
      console.warn("Could not load intelligence hub:", e);
    }
  }

  /** Лобби в режиме кубка: этапы сезона, затем линия и сетка текущего этапа. */
  async openCupLobby() {
    store.setLobbyMode('cup');
    store.setCupState({ loading: true, error: null });
    try {
      const res = await api.getCup();
      if (res.status !== 'ok') throw new Error(res.message || 'Кубок временно недоступен');
      const stages = res.stages || [];
      const keep = stages.some(s => s.id === store.state.cup.selectedStageId);
      const stageId = keep ? store.state.cup.selectedStageId : res.current_stage_id;
      store.setCupState({ stages, loading: false });
      if (stageId) await this.loadCupStage(stageId);
    } catch (err) {
      console.warn("Could not load cup:", err);
      store.setCupState({ loading: false, error: 'Не удалось загрузить кубок' });
    }
  }

  async loadCupStage(stageId) {
    store.setCupState({ selectedStageId: stageId, loading: true, error: null });
    const [lineRes, bracketRes] = await Promise.allSettled([
      api.getCupLine(stageId),
      api.getCupBracket(stageId)
    ]);
    // Пока шёл запрос, игрок мог выбрать другой этап — старый ответ не нужен.
    if (store.state.cup.selectedStageId !== stageId) return;
    const line = lineRes.status === 'fulfilled' && lineRes.value.status === 'ok' ? lineRes.value : null;
    const bracket = bracketRes.status === 'fulfilled' && bracketRes.value.status === 'ok' ? bracketRes.value : null;
    store.setCupState({
      line,
      bracket,
      loading: false,
      error: (line || bracket) ? null : 'Не удалось загрузить этап кубка'
    });
  }

  async fetchProgressionData() {
    try {
      const res = await api.getProgression();
      if (res.status === 'ok') {
        store.setProgression(res.progression, res.streak, res.unclaimed_achievements_count);
      }
      const achRes = await api.getAchievements();
      if (achRes.status === 'ok') {
        store.setAchievements(achRes.achievements);
      }
    } catch (e) {
      console.warn("Could not load progression:", e);
    }
  }

  /**
   * Рисует блок, только если его входные данные изменились с прошлого раза.
   * Подпись запоминается после успешной отрисовки, так что упавший блок повторится.
   */
  renderBlock(key, deps, render) {
    let sig;
    try {
      sig = JSON.stringify(deps);
    } catch (e) {
      sig = null;
    }
    if (sig !== null && this._renderSigs.get(key) === sig) return;
    try {
      render();
      if (sig !== null) this._renderSigs.set(key, sig);
    } catch (e) {
      this._renderSigs.delete(key);
      console.error(`Render error in ${key}:`, e);
    }
  }

  /** Сбрасывает подпись блока: следующий notify нарисует его заново. */
  invalidateRender(key) {
    this._renderSigs.delete(key);
  }

  renderTournamentTab(tab = null) {
    if (tab) this.currentTournamentTab = tab;
    const s = store.state;
    const deps = [s.standings, s.results, s.tournamentTopStats, this.currentTournamentTab, s.standingsForm, this.standingsSort, this.currentLeaderTab];
    this.renderBlock('tournaments', deps, () => UIRenderer.renderTournaments(
      s.standings,
      s.results,
      s.tournamentTopStats,
      this.currentTournamentTab,
      s.standingsForm,
      this.standingsSort,
      this.currentLeaderTab
    ));
  }

  async fetchTournamentData(divisionId = null) {
    try {
      const targetDiv = divisionId || store.state.selectedDivisionId || 1;
      const [stRes, resRes, topRes] = await Promise.all([
        api.getStandings(targetDiv),
        api.getResults(targetDiv),
        api.getTopScorers(targetDiv)
      ]);
      store.setTournamentData(
        stRes.status === 'ok' ? stRes.standings : [],
        resRes.status === 'ok' ? resRes.results : [],
        topRes.status === 'ok'
          ? {
              top_scorers: topRes.top_scorers || [],
              top_assists: topRes.top_assists || [],
              top_mvps: topRes.top_mvps || []
            }
          : { top_scorers: [], top_assists: [], top_mvps: [] },
        stRes.status === 'ok' ? (stRes.form || {}) : {}
      );
    } catch (e) {
      console.warn("Could not load tournament data:", e);
    }
  }

  async fetchUserExtras() {
    try {
      const [statsRes, savedRes, overviewRes] = await Promise.all([
        api.getMyStats(),
        api.getSavedCoupons(),
        api.getMyClubOverview().catch(() => null)
      ]);
      if (statsRes.status === 'ok') store.setMyStats(statsRes.stats);
      if (savedRes.status === 'ok') store.setSavedCoupons(savedRes.saved_coupons);
      if (overviewRes && overviewRes.status === 'ok') store.setMyClubOverview(overviewRes);
    } catch (e) {
      console.warn("Could not load user extras:", e);
    }
  }

  async fetchMyClubData() {
    // Баннер мог показывать ошибку прошлой загрузки — ближайший notify нарисует его заново.
    this.invalidateRender('myClubHero');
    store.setMyClubLoading(true);
    try {
      const overviewRes = await api.getMyClubOverview();
      if (overviewRes.status === 'ok') {
        store.setMyClubOverview(overviewRes);
        // Незаявленному игроку показываем онбординг — матчи и состав не грузим.
        if (!overviewRes.registered) return;
      }

      const [matchesRes, squadRes] = await Promise.all([
        api.getMyClubMatches().catch(() => null),
        api.getMyClubSquad().catch(() => null)
      ]);

      if (matchesRes && matchesRes.status === 'ok') {
        store.setMyClubMatches(matchesRes.matches || [], matchesRes.recent || []);
      }
      if (squadRes && squadRes.status === 'ok') {
        store.setMyClubSquad(squadRes.players || [], squadRes.top_scorer, squadRes.top_assistant, squadRes.top_mvp);
      }
    } catch (e) {
      console.warn("Could not load My Club data:", e);
      UIRenderer.renderMyClubError(e.message || "Ошибка подключения к серверу", () => this.fetchMyClubData());
    } finally {
      store.setMyClubLoading(false);
    }
  }

  async refreshMyClubMatches() {
    try {
      const res = await api.getMyClubMatches();
      if (res.status === 'ok') {
        store.setMyClubMatches(res.matches || [], res.recent || []);
      }
    } catch (e) {
      console.warn("Could not refresh My Club matches:", e);
    }
  }

  openMatchTimeModal(matchId, opponentName, currentTime) {
    const modal = document.getElementById('match-time-modal');
    if (!modal) return;

    this.pendingTimeMatchId = matchId;

    const opponentEl = document.getElementById('match-time-modal-opponent');
    if (opponentEl) opponentEl.textContent = `Соперник: ${opponentName || '—'}`;

    const errEl = document.getElementById('match-time-error');
    if (errEl) errEl.style.display = 'none';

    const dateInput = document.getElementById('match-time-date');
    const timeInput = document.getElementById('match-time-time');

    // Предзаполняем сегодняшней датой (локальной, без сдвига в UTC) и
    // ранее предложенным временем, если оно было.
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    if (dateInput && !dateInput.value) {
      dateInput.value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    }
    if (timeInput) {
      const match = (currentTime || '').match(/(\d{1,2}):(\d{2})/);
      timeInput.value = match ? `${pad(match[1])}:${match[2]}` : (timeInput.value || '20:00');
    }

    modal.classList.add('active');
  }

  async submitMatchTime() {
    const matchId = this.pendingTimeMatchId;
    const errEl = document.getElementById('match-time-error');
    const dateInput = document.getElementById('match-time-date');
    const timeInput = document.getElementById('match-time-time');
    const submitBtn = document.getElementById('btn-submit-match-time');

    const showError = (msg) => {
      if (errEl) {
        errEl.textContent = msg;
        errEl.style.display = 'block';
      } else {
        tgBridge.showAlert(msg);
      }
    };

    if (!matchId) return showError('Матч не выбран.');
    if (!timeInput || !timeInput.value) return showError('Укажите время начала матча.');

    // Формат «ДД.ММ.ГГГГ ЧЧ:ММ» — тот же, что понимает бот.
    let timeStr = timeInput.value;
    if (dateInput && dateInput.value) {
      const [y, m, d] = dateInput.value.split('-');
      timeStr = `${d}.${m}.${y} ${timeInput.value}`;
    }

    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api.proposeMatchTime(matchId, timeStr);
      if (res.status === 'ok') {
        const modal = document.getElementById('match-time-modal');
        if (modal) modal.classList.remove('active');
        tgBridge.hapticNotification('success');
        this.showSuccessModal('🗓 Время предложено', `Соперник получит предложение: ${timeStr}.`);
        await this.refreshMyClubMatches();
      }
    } catch (e) {
      showError(e.message || 'Не удалось отправить предложение времени.');
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  async openMatchProtocolModal(matchId) {
    const modal = document.getElementById('match-protocol-modal');
    if (!modal) return;

    UIRenderer.renderMatchProtocolModal(null);
    modal.classList.add('active');

    try {
      const res = await api.getMatchDetail(matchId);
      if (res.status === 'ok' && res.match) {
        UIRenderer.renderMatchProtocolModal(res);
      } else {
        const content = document.getElementById('match-protocol-content');
        if (content) {
          content.innerHTML = `<div class="load-error">Не удалось загрузить данные матча #${matchId}.</div>`;
        }
      }
    } catch (err) {
      const content = document.getElementById('match-protocol-content');
      if (content) {
        content.innerHTML = `<div class="load-error">${escapeHtml(err.message || 'Ошибка сети при загрузке протокола')}</div>`;
      }
    }
  }

  /** Открывает в Матч-Центре первый матч линии, если никакой ещё не выбран и не грузится. */
  ensureMatchCenterMatch() {
    if (store.state.selectedMatchId || this._matchCenterRequestedId != null) return;
    const [firstMatch] = UIRenderer.collectLineMatches(store.state.tours || []);
    if (firstMatch) this.loadMatchCenter(firstMatch.match_id);
  }

  async loadMatchCenter(matchId) {
    this._matchCenterRequestedId = matchId;
    try {
      const [detailRes, statsRes, h2hRes, insRes, liveRes, mktsRes] = await Promise.all([
        api.getMatchDetail(matchId),
        api.getMatchStats(matchId),
        api.getMatchH2H(matchId),
        api.getIntelligencePreview(matchId).catch(() => api.getMatchInsights(matchId)),
        api.getMatchLive(matchId),
        api.getMatchMarkets(matchId)
      ]);
      // Пока шёл запрос, пользователь открыл другой матч — этот ответ уже не нужен.
      if (this._matchCenterRequestedId !== matchId) return;

      store.setSelectedMatch(
        matchId,
        detailRes.status === 'ok' ? detailRes.match : null,
        statsRes.status === 'ok' ? statsRes : null,
        h2hRes.status === 'ok' ? h2hRes : null,
        insRes.status === 'ok' ? insRes : null,
        liveRes.status === 'ok' ? liveRes : null,
        mktsRes.status === 'ok' ? mktsRes.markets : []
      );
    } catch (e) {
      console.warn("Could not load match center:", e);
      // Разрешаем повторную попытку при следующем открытии вкладки.
      if (this._matchCenterRequestedId === matchId && !store.state.selectedMatchId) {
        this._matchCenterRequestedId = null;
      }
    }
  }

  bindEvents() {
    // 1. Navigation Tabs
    document.querySelectorAll('.nav-item').forEach(btn => {
      btn.addEventListener('click', () => {
        const view = btn.dataset.view;
        this.switchView(view);
      });
    });

    // 1b. Admin Panel (кнопка видна только админам; права всё равно проверяет сервер)
    document.getElementById('header-admin-btn')?.addEventListener('click', () => {
      this.switchView('admin');
    });

    // 2b. Division Selector Tabs (Lobby)
    const lobbyDivTabs = document.getElementById('lobby-division-tabs-container');
    if (lobbyDivTabs) {
      lobbyDivTabs.addEventListener('click', async (e) => {
        const cupBtn = e.target.closest('.cup-tab-btn');
        if (cupBtn) {
          tgBridge.hapticImpact('light');
          await this.openCupLobby();
          return;
        }
        const btn = e.target.closest('.division-tab-btn');
        if (btn && btn.dataset.divisionId) {
          const divId = parseInt(btn.dataset.divisionId);
          store.setLobbyMode('league');
          store.setSelectedDivisionId(divId);
          tgBridge.hapticImpact('light');
          try {
            const [toursData] = await Promise.all([
              api.getTours(divId),
              this.fetchTournamentData(divId),
              this.fetchIntelligenceHub(divId)
            ]);
            if (toursData.status === 'ok') {
              store.setTours(toursData.tours);
            }
          } catch (err) {
            console.error("Could not reload tours for division:", err);
          }
        }
      });
    }

    // 2c. Division Selector Tabs (Tournaments Hub)
    const tourDivTabs = document.getElementById('tournament-division-tabs-container');
    if (tourDivTabs) {
      tourDivTabs.addEventListener('click', async (e) => {
        const btn = e.target.closest('.division-tab-btn');
        if (btn && btn.dataset.divisionId) {
          const divId = parseInt(btn.dataset.divisionId);
          store.setSelectedDivisionId(divId);
          tgBridge.hapticImpact('light');
          await this.fetchTournamentData(divId);
        }
      });
    }

    // 3. Фильтр по турам удалён: лобби показывает единый список открытой линии.

    // 4. Кубок: выбор этапа и переключатель «Линия / Сетка»
    const cupView = document.getElementById('cup-view-container');
    if (cupView) {
      cupView.addEventListener('click', async (e) => {
        const stageBtn = e.target.closest('.cup-stage-chip');
        if (stageBtn && stageBtn.dataset.stageId) {
          const stageId = parseInt(stageBtn.dataset.stageId);
          tgBridge.hapticImpact('light');
          if (stageId !== store.state.cup.selectedStageId || store.state.cup.error) {
            await this.loadCupStage(stageId);
          }
          return;
        }
        const viewBtn = e.target.closest('.cup-view-btn');
        if (viewBtn && viewBtn.dataset.cupView) {
          tgBridge.hapticImpact('light');
          store.setCupState({ view: viewBtn.dataset.cupView });
        }
      });
    }

    // 5. Search Input
    const searchInput = document.getElementById('match-search-input');
    if (searchInput) {
      // Фильтр перестраивает списки матчей — не на каждую букву, а после паузы в наборе.
      let searchTimer = null;
      searchInput.addEventListener('input', (e) => {
        clearTimeout(searchTimer);
        const value = e.target.value;
        searchTimer = setTimeout(() => store.setSearchQuery(value), 150);
      });
    }

    // 6. Quick Odds Buttons on Match Cards & Match Center
    document.addEventListener('click', (e) => {
      const oddsBtn = e.target.closest('.odd-btn, .odds-btn');
      if (oddsBtn) {
        const mId = parseInt(oddsBtn.dataset.matchId);
        const outcome = oddsBtn.dataset.outcome;
        const odd = parseFloat(oddsBtn.dataset.odd);
        const mktId = oddsBtn.dataset.marketId ? parseInt(oddsBtn.dataset.marketId) : null;
        const selId = oddsBtn.dataset.selectionId ? parseInt(oddsBtn.dataset.selectionId) : null;
        const selName = oddsBtn.dataset.selectionName || null;
        const mktName = oddsBtn.dataset.marketName || null;

        // Find match object in tours or active match detail
        let targetMatch = null;
        for (const t of store.state.tours) {
          const found = (t.matches || []).find(m => m.match_id === mId || m.id === mId);
          if (found) {
            targetMatch = found;
            break;
          }
        }
        // Кубок: тайл линии этапа — у заголовка серии имена пары берутся из серии.
        let slipMeta = oddsBtn.dataset.slipMeta || null;
        if (!targetMatch) {
          const cupTile = store.findCupTile(mId);
          if (cupTile) {
            targetMatch = cupTile;
            if (!slipMeta) {
              const label = cupStageLabel(store.state.cup.line?.stage?.stage);
              slipMeta = cupTile.is_series_header
                ? `Кубок · ${label} · серия`
                : `Кубок · ${label} · игра ${cupTile.game_num_in_series}`;
            }
          }
        }
        if (!targetMatch && store.state.matchDetail && (store.state.matchDetail.id === mId || store.state.matchDetail.match_id === mId)) {
          targetMatch = store.state.matchDetail;
        }
        if (!targetMatch) {
          targetMatch = { match_id: mId, team1_name: 'Хозяева', team2_name: 'Гости', tour: 1 };
        }

        store.toggleSelection(targetMatch, outcome, odd, {
          market_id: mktId,
          selection_id: selId,
          selection_name: selName,
          market_name: mktName,
          meta: slipMeta
        });

        // Update selection highlight in open modal if any
        document.querySelectorAll('#modal-markets-list .odd-btn').forEach(b => {
          const bMId = parseInt(b.dataset.matchId);
          const bOutcome = b.dataset.outcome;
          b.classList.toggle('selected', store.isSelectionActive(bMId, bOutcome));
        });

        tgBridge.hapticImpact('light');
      }
    });

    // 7. Match Center Sub-Tabs Switching
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.mc-subtab-btn');
      if (btn && btn.dataset.subtab) {
        store.setMatchCenterSubTab(btn.dataset.subtab);
        tgBridge.hapticImpact('light');
      }
    });

    // 7. Open Match Center from card
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-open-match-center');
      if (btn && btn.dataset.matchId) {
        const mId = parseInt(btn.dataset.matchId);
        const modal = document.getElementById('match-markets-modal');
        if (modal) {
          modal.classList.remove('active', 'open');
          modal.style.display = 'none';
        }
        this.loadMatchCenter(mId);
        this.switchView('match_center');
      }
    });

    // 8. Open All Markets Modal
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-more-markets');
      if (btn && btn.dataset.matchId) {
        const mId = parseInt(btn.dataset.matchId);
        const modal = document.getElementById('match-markets-modal');
        if (modal) {
          modal.classList.add('active', 'open');
          modal.style.display = 'flex';
          const titleEl = document.getElementById('modal-match-title');
          const listEl = document.getElementById('modal-markets-list');
          if (titleEl) titleEl.textContent = 'Все рынки матча';
          if (listEl) {
            listEl.innerHTML = '<div class="markets-loading"><div class="markets-loading-icon">⏳</div>Загрузка доступных котировок...</div>';
          }
          try {
            const data = await api.getMatchMarkets(mId);
            if (data.status === 'ok') {
              UIRenderer.renderMatchMarketsModal(mId, data.markets, `${data.team1_name} — ${data.team2_name}`);
            } else {
              if (listEl) listEl.innerHTML = `<div class="markets-error">${escapeHtml(data.message || 'Рынки временно недоступны')}</div>`;
            }
          } catch (err) {
            console.error("Could not load markets:", err);
            if (listEl) listEl.innerHTML = '<div class="markets-error">Ошибка связи с сервером</div>';
          }
        }
      }
    });

    // 9. Tournament Sub-tabs
    const btnStandings = document.getElementById('btn-tab-standings');
    const btnResults = document.getElementById('btn-tab-results');
    const btnScorers = document.getElementById('btn-tab-scorers');

    if (btnStandings && btnResults && btnScorers) {
      const setTab = (tab, activeBtn) => {
        this.currentTournamentTab = tab;
        [btnStandings, btnResults, btnScorers].forEach(b => b.classList.remove('active'));
        activeBtn.classList.add('active');
        this.renderTournamentTab(tab);
        tgBridge.hapticImpact('light');
      };

      btnStandings.addEventListener('click', () => setTab('standings', btnStandings));
      btnResults.addEventListener('click', () => setTab('results', btnResults));
      btnScorers.addEventListener('click', () => setTab('scorers', btnScorers));
    }

    // 9b. Сортировка таблицы: делегированный клик по шапке (она перерисовывается)
    const tournamentsContainer = document.getElementById('tournaments-content-container');
    if (tournamentsContainer) {
      tournamentsContainer.addEventListener('click', (e) => {
        // 9c. Переключатель списков лидеров (бомбардиры / ассистенты / MVP).
        // Кнопки живут внутри перерисовываемого контейнера — только делегирование.
        const leaderBtn = e.target.closest('[data-leader-tab]');
        if (leaderBtn) {
          this.currentLeaderTab = leaderBtn.dataset.leaderTab;
          this.renderTournamentTab(this.currentTournamentTab);
          tgBridge.hapticImpact('light');
          return;
        }

        const th = e.target.closest('th[data-sort-key]');
        if (!th) return;
        const key = th.dataset.sortKey;
        if (this.standingsSort.key === key) {
          this.standingsSort.dir = this.standingsSort.dir === 'desc' ? 'asc' : 'desc';
        } else {
          // Клуб сортируем по алфавиту, числовые колонки — сразу от большего.
          this.standingsSort = { key, dir: key === 'team' ? 'asc' : 'desc' };
        }
        this.renderTournamentTab(this.currentTournamentTab);
        tgBridge.hapticImpact('light');
      });
    }

    // 10. History Filter Chips
    const historyFilters = document.getElementById('history-filter-pills');
    if (historyFilters) {
      historyFilters.addEventListener('click', async (e) => {
        const btn = e.target.closest('.category-pill');
        if (btn && btn.dataset.filter) {
          const filter = btn.dataset.filter;
          historyFilters.querySelectorAll('.category-pill').forEach(p => p.classList.remove('active'));
          btn.classList.add('active');
          store.setMyBets(store.state.myBets, filter);
          tgBridge.hapticImpact('light');

          try {
            const res = await api.getPredictions(filter === 'all' ? null : filter, 50);
            if (res && res.status === 'ok') {
              store.setMyBets(res.predictions || res.bets || [], filter);
            }
          } catch (err) {
            console.warn("Could not load filtered predictions:", err);
          }
        }
      });
    }

    // 11. Repeat Prediction Button
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-repeat-bet');
      if (btn && btn.dataset.betId) {
        try {
          const res = await api.repeatPrediction(parseInt(btn.dataset.betId));
          if (res.status === 'ok') {
            store.loadCouponSelections(res.selections);
            store.setStakeAmount(res.amount);
            this.toggleSlipDrawer(true);
            this.showSuccessModal('🔄 Прогноз скопирован!', res.message);
          }
        } catch (err) {
          tgBridge.showAlert(err.message);
        }
      }
    });

    // 12. Save Draft Coupon
    const saveSlipBtn = document.getElementById('btn-save-draft-slip');
    if (saveSlipBtn) {
      saveSlipBtn.addEventListener('click', async () => {
        if (store.state.slip.length === 0) {
          tgBridge.showAlert("Купон пуст. Выберите хотя бы один исход.");
          return;
        }
        try {
          const res = await api.saveCoupon(
            `Экспресс (${store.state.slip.length})`,
            store.state.slip,
            store.getTotalOdd()
          );
          if (res.status === 'ok') {
            this.fetchUserExtras();
            this.showSuccessModal('💾 Черновик сохранен', res.message);
          }
        } catch (err) {
          tgBridge.showAlert(err.message);
        }
      });
    }

    // 13. Restore / Delete Saved Coupon
    document.addEventListener('click', async (e) => {
      const restBtn = e.target.closest('.btn-restore-coupon');
      if (restBtn && restBtn.dataset.savedId) {
        const sId = parseInt(restBtn.dataset.savedId);
        const matchSaved = store.state.savedCoupons.find(s => s.id === sId);
        if (matchSaved && matchSaved.selections) {
          store.loadCouponSelections(matchSaved.selections);
          this.toggleSlipDrawer(true);
        }
      }

      const delBtn = e.target.closest('.btn-delete-saved-coupon');
      if (delBtn && delBtn.dataset.savedId) {
        try {
          await api.deleteSavedCoupon(parseInt(delBtn.dataset.savedId));
          this.fetchUserExtras();
        } catch (err) {
          console.warn("Delete saved coupon error:", err);
        }
      }
    });

    // 13b. LIVE-центр удалён из мини-приложения — обработчиков нет.
    // Лайв-данные конкретного матча по-прежнему доступны во вкладке Матч-Центра.

    // 14. Bet Coupon: floating bar + bottom sheet
    const betbar = document.getElementById('betbar');
    if (betbar) {
      betbar.addEventListener('click', () => this.toggleSlipDrawer(true));
      betbar.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          this.toggleSlipDrawer(true);
        }
      });
    }
    document.getElementById('btn-close-coupon')?.addEventListener('click', () => this.toggleSlipDrawer(false));
    document.getElementById('coupon-backdrop')?.addEventListener('click', () => this.toggleSlipDrawer(false));
    this.bindCouponSwipe();

    const clearSlipBtn = document.getElementById('btn-clear-slip');
    if (clearSlipBtn) {
      clearSlipBtn.addEventListener('click', () => {
        store.clearSlip();
      });
    }

    // Segmented control: Ординар / Экспресс
    document.addEventListener('click', (e) => {
      const modeBtn = e.target.closest('.slip-type-btn');
      if (!modeBtn || !modeBtn.dataset.slipMode) return;
      if (modeBtn.dataset.slipMode === 'express' && store.state.slip.length < 2) {
        tgBridge.hapticNotification('warning');
        return;
      }
      if (store.getSlipMode() !== modeBtn.dataset.slipMode) {
        store.setSlipMode(modeBtn.dataset.slipMode);
        tgBridge.hapticImpact('light');
      }
    });

    document.addEventListener('click', (e) => {
      const rmBtn = e.target.closest('.btn-remove-slip-item');
      if (rmBtn && rmBtn.dataset.matchId) {
        store.removeSelection(parseInt(rmBtn.dataset.matchId));
      }
    });

    // Quick stake chips: +N adds to the stake, MAX fills the rules' maximum
    document.querySelectorAll('.coupon-chips .stake-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        if (chip.dataset.amount === 'max') {
          store.setStakeAmount(store.getMaxStake());
        } else {
          store.addToStake(parseInt(chip.dataset.add) || 0);
        }
        tgBridge.hapticImpact('light');
      });
    });

    const stakeInput = document.getElementById('stake-input');
    if (stakeInput) {
      stakeInput.addEventListener('input', (e) => {
        store.setStakeAmount(parseInt(e.target.value) || 0);
      });
      // The render skips a focused input; normalise it once the user leaves it.
      stakeInput.addEventListener('blur', () => store.notify());
    }

    // Per-event stakes (batch singles)
    document.addEventListener('input', (e) => {
      const input = e.target.closest?.('.coupon-single-stake');
      if (input && input.dataset.matchId) {
        store.setSingleStake(parseInt(input.dataset.matchId), input.value);
      }
    });
    document.addEventListener('focusout', (e) => {
      if (e.target.closest?.('.coupon-single-stake')) store.notify();
    });

    // Submit CTA
    const submitBtn = document.getElementById('btn-submit-prediction');
    if (submitBtn) {
      submitBtn.addEventListener('click', async () => {
        const slip = store.state.slip;
        if (slip.length === 0 || submitBtn.classList.contains('loading')) return;

        const { min_bet } = store.getBetLimits();
        const isSingleBatch = store.isBatchSingles();
        const stakes = new Map(slip.map(s => [s.match_id, isSingleBatch ? store.getSingleStake(s.match_id) : store.state.stakeAmount]));
        const totalAmt = store.getTotalStake();

        if ([...stakes.values()].some(v => v < min_bet)) {
          tgBridge.showAlert(`Минимальная сумма ставки — ${min_bet} 🪙.`);
          return;
        }
        if ((store.state.user?.balance || 0) < totalAmt) {
          tgBridge.showAlert(`Недостаточно монет на балансе (необходимо ${totalAmt} 🪙).`);
          return;
        }

        tgBridge.hapticImpact('heavy');
        submitBtn.classList.add('loading');
        submitBtn.disabled = true;
        const ctaMain = document.getElementById('coupon-cta-main');
        const ctaSub = document.getElementById('coupon-cta-sub');
        if (ctaMain) ctaMain.innerHTML = '<span class="coupon-spinner"></span>Принятие пари...';
        if (ctaSub) ctaSub.textContent = '';

        const refreshBets = async () => {
          this.fetchUserExtras();
          try {
            const myBetsRes = await api.getPredictions();
            if (myBetsRes.status === 'ok') store.setMyBets(myBetsRes.predictions);
          } catch (e) {
            console.warn("Could not refresh predictions:", e);
          }
        };

        try {
          if (isSingleBatch) {
            const placed = [];
            const failedItems = [];

            for (const item of [...slip]) {
              const amt = stakes.get(item.match_id);
              const key = `slip-single-${Date.now()}-${item.match_id}-${Math.random().toString(36).substring(2, 6)}`;
              try {
                const res = await api.placePrediction(amt, [item], key);
                if (res.status === 'ok') {
                  placed.push({ id: res.bet_id, amt, win: Math.round(amt * item.odd) });
                  store.addOpenExposure(Math.round(amt * item.odd));
                  store.addOpenBet();
                  if (res.new_balance !== undefined) {
                    store.setUser({ ...store.state.user, balance: res.new_balance });
                  }
                  // Placed bets leave the coupon right away; failures stay for a retry.
                  store.removeSelection(item.match_id);
                } else {
                  failedItems.push({ item, error: res.message || 'Ошибка размещения ставки' });
                }
              } catch (err) {
                // Слоты кончились — сервер назвал точное число; берём его,
                // чтобы счётчик в купоне не врал до следующего bootstrap.
                if (err.data?.error === 'OPEN_BETS_LIMIT') {
                  store.setOpenBets(err.data.open_bets, err.data.max_open_bets);
                }
                failedItems.push({ item, error: err.data?.message || err.message || 'Не удалось разместить ставку' });
              }
            }

            if (placed.length > 0) {
              refreshBets();
              this.showBetAccepted({
                title: failedItems.length ? `Принято ${placed.length} из ${placed.length + failedItems.length}` : 'Пари принято!',
                ids: placed.map(p => p.id),
                stake: placed.reduce((s, p) => s + p.amt, 0),
                oddLabel: 'Ординаров',
                oddValue: String(placed.length),
                win: placed.reduce((s, p) => s + p.win, 0)
              });
            }
            if (failedItems.length > 0) {
              const errDetails = failedItems.map(f => `• ${f.item.team1_name} — ${f.item.team2_name}: ${f.error}`).join('\n');
              tgBridge.showAlert(`Не удалось принять (${failedItems.length}):\n${errDetails}`);
            }
          } else {
            const amt = store.state.stakeAmount;
            const totalOdd = store.getTotalOdd();
            const isExp = slip.length > 1;
            const idempotencyKey = `slip-${Date.now()}-${Math.random().toString(36).substring(2, 8)}`;
            const res = await api.placePrediction(amt, slip, idempotencyKey);
            if (res.status === 'ok') {
              store.setUser({ ...store.state.user, balance: res.new_balance });
              store.addOpenExposure(Math.round(amt * totalOdd));
              store.addOpenBet();
              store.clearSlip();
              refreshBets();
              this.showBetAccepted({
                title: isExp ? 'Экспресс принят!' : 'Пари принято!',
                ids: [res.bet_id],
                stake: amt,
                oddLabel: isExp ? 'Общий кэф' : 'Коэффициент',
                oddValue: totalOdd.toFixed(2),
                win: Math.round(amt * totalOdd)
              });
            }
          }
        } catch (err) {
          if (err.data && err.data.error === 'OPEN_BETS_LIMIT') {
            store.setOpenBets(err.data.open_bets, err.data.max_open_bets);
          }
          if (err.data && err.data.error === 'ODDS_CHANGED') {
            const { old_odd, new_odd, match_id, outcome } = err.data;
            UIRenderer.showOddsChangedModal(
              old_odd,
              new_odd,
              () => {
                // User accepted new odds
                const item = store.state.slip.find(s => s.match_id === match_id && s.outcome === outcome);
                if (item) {
                  item.odd = parseFloat(new_odd);
                  store.notify();
                }
                tgBridge.hapticImpact('medium');
                // Allow UI to re-enable before triggering re-submission
                setTimeout(() => {
                  submitBtn.click();
                }, 100);
              },
              () => {
                tgBridge.hapticImpact('light');
              }
            );
            return;
          }
          tgBridge.hapticNotification('error');
          tgBridge.showAlert(err.message);
        } finally {
          submitBtn.classList.remove('loading');
          submitBtn.disabled = false;
          // Force the CTA text back from the loader: the render only writes changed text.
          if (ctaMain && ctaMain.innerHTML.includes('coupon-spinner')) {
            ctaMain.textContent = '';
          }
          store.notify();
          // Safety guard: ensure the button NEVER stays completely blank
          if (ctaMain && !ctaMain.textContent.trim()) {
            ctaMain.textContent = 'Поставить';
          }
        }
      });
    }

    document.getElementById('btn-bet-accepted-history')?.addEventListener('click', () => {
      const modal = document.getElementById('bet-accepted-modal');
      if (modal) {
        modal.classList.remove('active', 'open');
        modal.style.display = 'none';
      }
      this.switchView('history');
    });

    // 15. Modals close triggers
    document.querySelectorAll('.modal-overlay').forEach(modal => {
      modal.addEventListener('click', (e) => {
        if (e.target === modal || e.target.closest('.btn-modal-close')) {
          modal.classList.remove('active', 'open');
          modal.style.display = 'none';
        }
      });
    });

    // 16. Leaderboard Modal Trigger
    const btnLdr = document.getElementById('btn-toggle-leaderboard-modal');
    if (btnLdr) {
      btnLdr.addEventListener('click', async () => {
        const modal = document.getElementById('leaderboard-modal');
        if (modal) {
          modal.classList.add('active');
          try {
            const data = await api.getLeaderboard();
            if (data.status === 'ok') {
              // Модалка — «зал славы» по монетам: рендерер читает p.balance,
              // а баланс есть только в leaders. entries/leaderboard оставлены
              // запасным вариантом на случай смены источника на сервере.
              const rows = data.leaders || data.entries || data.leaderboard || [];
              const myRank = (data.user_pin && data.user_pin.rank) || data.my_rank || null;
              UIRenderer.renderLeaderboardModal(rows, myRank);
            }
          } catch (err) {
            console.warn("Could not load leaderboard:", err);
          }
        }
      });
    }

    // 17. Achievement Reward Claim
    // Ответ /api/achievements/claim не содержит нового баланса, поэтому
    // кошелёк перечитывается отдельно — иначе шапка показывает старые монеты.
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-claim-achievement');
      if (!btn || !btn.dataset.claimAchId) return;
      btn.disabled = true;
      try {
        const res = await api.claimAchievement(btn.dataset.claimAchId);
        if (res.status !== 'ok') {
          tgBridge.showAlert(res.message || 'Не удалось получить награду.');
          return;
        }
        tgBridge.showAlert(res.message || 'Награда получена!');
        try {
          const walletRes = await api.getWallet();
          if (walletRes.status === 'ok' && walletRes.wallet) {
            store.setUser({ ...store.state.user, balance: walletRes.wallet.balance });
          }
        } catch (err2) {
          console.warn("Could not refresh wallet after claim:", err2);
        }
        await this.fetchProgressionData();
      } catch (err) {
        tgBridge.showAlert(err.message || 'Не удалось получить награду.');
      } finally {
        btn.disabled = false;
      }
    });

    // 18. Early Cashout Settlement
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-cashout');
      if (btn && btn.dataset.betId) {
        const betId = parseInt(btn.dataset.betId);
        btn.disabled = true;
        try {
          const quoteRes = await api.getCashoutQuote(betId);
          // Сервер отдаёт котировку вложенной в quote; вариант без вложения
          // оставлен на случай ответа старого формата.
          const quote = quoteRes.quote || quoteRes;
          if (quoteRes.status !== 'ok' || !quote.cashout_available) {
            tgBridge.showAlert(quoteRes.message || "Кэшаут в данный момент недоступен для этого прогноза.");
            return;
          }
          const quoteAmount = quote.amount;
          const confirmLines = [`💰 Кэшаут ставки #${betId}`, ''];
          if (quote.stake) confirmLines.push(`Ставка: ${quote.stake} 🪙`);
          if (quote.potential_win) confirmLines.push(`Возможный выигрыш: ${quote.potential_win} 🪙`);
          confirmLines.push(`Получите сейчас: ${quoteAmount} 🪙`);
          if (quote.stake) {
            const diff = quoteAmount - quote.stake;
            confirmLines.push(diff >= 0 ? `Прибыль: +${diff} 🪙` : `Итог к ставке: −${-diff} 🪙`);
          }
          confirmLines.push('', 'Завершить ставку досрочно?');
          tgBridge.showConfirm(
            confirmLines.join('\n'),
            async (confirmed) => {
              if (!confirmed) return;
              try {
                const idempotencyKey = `co-${betId}-${Date.now()}`;
                const execRes = await api.executeCashout(betId, idempotencyKey);
                if (execRes.status === 'ok') {
                  const res = execRes.result || execRes;
                  const newBal = res.new_balance;
                  store.setUser({ ...store.state.user, balance: newBal });
                  tgBridge.hapticNotification('success');
                  const doneLines = [`Ставка #${betId} закрыта досрочно.`, `Зачислено: +${res.payout} 🪙`];
                  if (res.stake) {
                    const diff = res.payout - res.stake;
                    doneLines.push(diff >= 0 ? `Прибыль: +${diff} 🪙` : `Итог к ставке: −${-diff} 🪙`);
                  }
                  if (newBal !== undefined && newBal !== null) doneLines.push(`Баланс: ${newBal} 🪙`);
                  this.showSuccessModal('💰 Кэшаут выполнен!', doneLines.join('\n'));
                  try {
                    const myBetsRes = await api.getPredictions();
                    if (myBetsRes.status === 'ok') store.setMyBets(myBetsRes.predictions);
                  } catch (err2) {
                    console.warn("Could not refresh predictions after cashout:", err2);
                  }
                  this.fetchUserExtras();
                }
              } catch (execErr) {
                tgBridge.showAlert(execErr.message || "Не удалось выполнить кэшаут.");
              }
            }
          );
        } catch (err) {
          tgBridge.showAlert(err.message || "Ошибка получения котировки кэшаута.");
        } finally {
          btn.disabled = false;
        }
      }
    });

    // 18b. My Club — внутренние под-вкладки
    document.querySelectorAll('#my-club-subtabs .mc-subtab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        store.setMyClubSubTab(btn.dataset.clubTab);
        tgBridge.hapticImpact('light');
      });
    });

    // 18c. My Club — согласование времени матча
    document.addEventListener('click', async (e) => {
      const proposeBtn = e.target.closest('.btn-propose-time');
      if (proposeBtn) {
        this.openMatchTimeModal(
          parseInt(proposeBtn.dataset.matchId),
          proposeBtn.dataset.opponent,
          proposeBtn.dataset.currentTime
        );
        tgBridge.hapticImpact('light');
        return;
      }

      const acceptBtn = e.target.closest('.btn-accept-time');
      if (acceptBtn) {
        const matchId = parseInt(acceptBtn.dataset.matchId);
        acceptBtn.disabled = true;
        try {
          const res = await api.acceptMatchTime(matchId);
          if (res.status === 'ok') {
            tgBridge.hapticNotification('success');
            this.showSuccessModal('✅ Время согласовано', `Матч назначен на ${res.proposed_time || 'согласованное время'}.`);
            await this.refreshMyClubMatches();
          }
        } catch (err) {
          tgBridge.showAlert(err.message || 'Не удалось подтвердить время матча.');
        } finally {
          acceptBtn.disabled = false;
        }
      }

      const protocolBtn = e.target.closest('.btn-view-match-protocol') || e.target.closest('.club-match-card.clickable');
      if (protocolBtn && protocolBtn.dataset.matchId) {
        const matchId = parseInt(protocolBtn.dataset.matchId);
        if (matchId) {
          this.openMatchProtocolModal(matchId);
          tgBridge.hapticImpact('light');
          return;
        }
      }
    });

    // 18d. My Club — пресеты и отправка в модалке выбора времени
    document.querySelectorAll('#match-time-presets .time-preset-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const timeInput = document.getElementById('match-time-time');
        if (timeInput) timeInput.value = btn.dataset.time;
        document.querySelectorAll('#match-time-presets .time-preset-btn')
          .forEach(b => b.classList.toggle('active', b === btn));
      });
    });

    const btnSubmitTime = document.getElementById('btn-submit-match-time');
    if (btnSubmitTime) {
      btnSubmitTime.addEventListener('click', () => this.submitMatchTime());
    }

    // 19. Close Locked App screen
    const btnCloseLocked = document.getElementById('btn-close-locked-app');
    if (btnCloseLocked) {
      btnCloseLocked.addEventListener('click', () => {
        tgBridge.close();
      });
    }

    // 20. Close Global Lockdown App screen
    const btnCloseLockdown = document.getElementById('btn-close-lockdown-app');
    if (btnCloseLockdown) {
      btnCloseLockdown.addEventListener('click', () => {
        try {
          tgBridge.close();
        } catch (e) {
          window.close();
        }
      });
    }
  }

  isCouponOpen() {
    return !!document.getElementById('coupon-sheet')?.classList.contains('open');
  }

  toggleSlipDrawer(forceOpen = null) {
    const sheet = document.getElementById('coupon-sheet');
    const backdrop = document.getElementById('coupon-backdrop');
    if (!sheet) return;

    const open = forceOpen === null ? !this.isCouponOpen() : !!forceOpen;
    if (open && store.state.slip.length === 0) return;
    if (open === this.isCouponOpen()) return;

    sheet.classList.toggle('open', open);
    sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
    backdrop?.classList.toggle('open', open);
    document.body.classList.toggle('coupon-open', open);

    if (open) {
      tgBridge.hapticImpact('medium');
      tgBridge.showBackButton(() => this.toggleSlipDrawer(false));
    } else {
      tgBridge.hideBackButton();
      document.activeElement?.blur?.();
    }
    // The bar hides while the sheet is open and comes back when it closes.
    UIRenderer.renderSlipDrawer(store.state.slip, store.state.stakeAmount);
  }

  /** Swipe the sheet down by its handle or header to close it. */
  bindCouponSwipe() {
    const sheet = document.getElementById('coupon-sheet');
    if (!sheet) return;
    const zones = [document.getElementById('coupon-grab'), sheet.querySelector('.coupon-head')].filter(Boolean);
    let startY = null;
    let dy = 0;

    const onMove = (e) => {
      if (startY === null) return;
      dy = Math.max(0, e.clientY - startY);
      sheet.style.transform = `translateY(${dy}px)`;
    };
    const onEnd = () => {
      if (startY === null) return;
      startY = null;
      sheet.classList.remove('dragging');
      sheet.style.transform = '';
      if (dy > 100) this.toggleSlipDrawer(false);
      dy = 0;
    };

    zones.forEach(zone => {
      zone.addEventListener('pointerdown', (e) => {
        // Buttons in the header keep their clicks: capturing would retarget them.
        if (e.target.closest('button')) return;
        startY = e.clientY;
        dy = 0;
        sheet.classList.add('dragging');
        zone.setPointerCapture?.(e.pointerId);
      });
      zone.addEventListener('pointermove', onMove);
      zone.addEventListener('pointerup', onEnd);
      zone.addEventListener('pointercancel', onEnd);
    });
  }

  showBetAccepted({ title, ids, stake, oddLabel, oddValue, win }) {
    this.toggleSlipDrawer(false);
    const modal = document.getElementById('bet-accepted-modal');
    if (!modal) return;

    const fmt = (n) => (n || 0).toLocaleString('ru-RU');
    const numbers = (ids || []).filter(Boolean).map(id => `#${id}`);
    const titleEl = document.getElementById('bet-accepted-title');
    const couponEl = document.getElementById('bet-accepted-coupon');
    const detailsEl = document.getElementById('bet-accepted-details');
    if (titleEl) titleEl.textContent = title;
    if (couponEl) {
      couponEl.textContent = numbers.length === 0 ? ''
        : numbers.length === 1 ? `Номер купона ${numbers[0]}` : `Купоны ${numbers.join(', ')}`;
    }
    if (detailsEl) {
      detailsEl.innerHTML = `
        <div class="bet-accepted-stat"><span>Ставка</span><b>${fmt(stake)} 🪙</b></div>
        <div class="bet-accepted-stat"><span>${oddLabel}</span><b>${oddValue}</b></div>
        <div class="bet-accepted-stat win bet-accepted-stat-wide"><span>Возможный выигрыш</span><b>${fmt(win)} 🪙</b></div>`;
    }

    // Restart the check-mark drawing animation on every show.
    const svg = modal.querySelector('.bet-accepted-check svg');
    if (svg) svg.replaceWith(svg.cloneNode(true));

    modal.style.display = '';
    modal.classList.add('active');
    tgBridge.hapticNotification('success');
    // Cosmetic only: a failing effect must not surface as a bet error in the caller's catch.
    try { ParticleEffects.burstConfetti(); } catch (e) { console.warn('confetti failed', e); }
  }

  switchView(viewName) {
    store.setActiveView(viewName);

    // Update bottom nav
    document.querySelectorAll('.bottom-nav .nav-item').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === viewName);
    });

    // Update views container
    document.querySelectorAll('.view-section').forEach(sec => {
      sec.classList.toggle('active', sec.id === `view-${viewName}`);
    });

    // On-demand view refresh
    if (viewName === 'history') {
      const activeFilter = document.querySelector('#history-filter-pills .category-pill.active')?.dataset.filter || 'all';
      api.getPredictions(activeFilter === 'all' ? null : activeFilter, 50).then(res => {
        if (res.status === 'ok') store.setMyBets(res.predictions || res.bets || [], activeFilter);
      }).catch(() => {});
    } else if (viewName === 'profile') {
      this.fetchUserExtras();
    } else if (viewName === 'tournaments') {
      this.fetchTournamentData();
    } else if (viewName === 'my_club') {
      this.fetchMyClubData();
    } else if (viewName === 'match_center') {
      this.ensureMatchCenterMatch();
    } else if (viewName === 'admin') {
      this.openAdminPanel();
    }

    document.getElementById('header-admin-btn')?.classList.toggle('active', viewName === 'admin');
    tgBridge.hapticImpact('light');
  }

  openAdminPanel() {
    if (!this.adminPanel) {
      const root = document.getElementById('admin-root');
      const modal = document.getElementById('admin-modal');
      if (!root || !modal) return;
      this.adminPanel = new AdminPanel(root, modal, {
        // Сборщик купона из «ИИ-прогноза»: события — в купон, ставку админ подтверждает сам.
        toCoupon: (items, mode) => {
          const apply = () => {
            store.loadCouponSelections(items);
            store.setSlipMode(mode);
            this.toggleSlipDrawer(true);
          };
          const current = store.state.slip.length;
          if (!current) return apply();
          tgBridge.showConfirm(`Заменить текущий купон (событий: ${current}) собранным?`, ok => { if (ok) apply(); });
        },
      });
    }
    this.adminPanel.open();
  }

  showSuccessModal(title, desc) {
    const modal = document.getElementById('general-success-modal');
    const titleEl = document.getElementById('success-modal-title');
    const descEl = document.getElementById('success-modal-desc');
    if (modal) {
      if (titleEl) titleEl.textContent = title;
      if (descEl) {
        descEl.textContent = desc;
        descEl.style.whiteSpace = 'pre-line';
      }
      modal.classList.add('active');
    }
  }
}

// Instantiate on DOM ready
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', () => {
    new AppController();
  });
} else {
  new AppController();
}
