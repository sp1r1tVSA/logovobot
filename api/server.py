"""
api/server.py

Embedded async HTTP server (aiohttp.web) for Logovo.bet Telegram Mini App.
Serves REST API and static single-page application assets.
"""

import os
import asyncio
import logging
from aiohttp import hdrs, web

from api.routes_wallet import (
    handle_bootstrap,
    handle_claim_bonus,
    handle_leaderboard,
    handle_get_division_leaderboard,
    handle_get_wallet,
)
from api.routes_markets import handle_get_tours, handle_get_match_markets, handle_get_odds_history
from api.routes_cup import handle_get_cup, handle_get_cup_bracket, handle_get_cup_line
from api.routes_predictions import (
    handle_place_prediction,
    handle_get_predictions,
    handle_get_prediction_detail,
    handle_repeat_prediction,
    handle_get_cashout_quote,
    handle_execute_cashout
)
from api.routes_admin_risk import (
    handle_admin_get_exposure,
    handle_admin_get_risk_alerts,
    handle_admin_ack_alert,
    handle_admin_resolve_alert,
    handle_admin_get_limits,
    handle_admin_set_limits,
    handle_admin_emergency_suspend
)
from api.routes_matches import (
    handle_get_matches,
    handle_get_match_detail,
    handle_get_match_photo,
    handle_get_match_stats,
    handle_get_match_h2h,
    handle_get_match_insights,
    handle_get_match_live,
    handle_get_hot_matches,
    handle_get_recommendations,
)
from api.routes_tournaments import (
    handle_get_tournaments,
    handle_get_divisions,
    handle_get_seasons,
    handle_get_season_by_id,
    handle_get_standings,
    handle_get_results,
    handle_get_top_scorers,
    handle_get_my_tournament_stats
)
from api.routes_player_cabinet import (
    handle_get_cabinet_overview,
    handle_get_cabinet_matches,
    handle_get_cabinet_squad,
    handle_post_cabinet_match_time,
)
from api.routes_tracker import (
    handle_tracker_pair,
    handle_tracker_matches,
    handle_tracker_session_start,
    handle_tracker_session_tick,
    handle_tracker_session_event,
    handle_tracker_session_finish,
)
from api.routes_user_extras import (
    handle_get_my_stats,
    handle_get_profile_analytics,
    handle_save_coupon,
    handle_get_saved_coupons,
    handle_delete_saved_coupon,
    handle_add_favorite,
    handle_get_favorites,
    handle_delete_favorite,
    handle_get_notifications,
    handle_mark_notifications_read
)
from api.routes_gamification import (
    handle_get_progression,
    handle_get_achievements,
    handle_claim_achievement,
    handle_get_profile,
    handle_get_player_public,
    handle_get_profile_stats,
    handle_get_leaderboard,
    handle_get_leaderboard_division,
    handle_get_leaderboard_season,
    handle_get_season,
    handle_get_season_rewards,
)
from api.routes_admin_season import (
    handle_admin_get_season,
    handle_admin_create_season,
    handle_admin_finalize_season,
    handle_admin_season_rewards,
)
from api.routes_admin_betting import (
    handle_admin_list_markets,
    handle_admin_transition_market,
    handle_admin_update_odds,
    handle_admin_list_bets,
    handle_admin_void_bet,
    handle_admin_audit_log,
)
from api.routes_live import (
    handle_get_live_matches,
    handle_get_live_match_detail,
    handle_get_live_events,
    handle_get_live_stats,
    handle_get_live_markets,
    handle_get_odds_movers,
    handle_get_live_intelligence,
)
from api.routes_admin_live import (
    handle_admin_live_overview,
    handle_admin_suspend_market,
    handle_admin_resume_market,
    handle_admin_close_market,
    handle_admin_void_market,
    handle_admin_match_correction,
    handle_admin_refresh_match,
    handle_admin_sports_health,
)
from api.routes_intelligence import (
    handle_get_intelligence_matches,
    handle_get_intelligence_match_detail,
    handle_get_intelligence_preview,
    handle_get_intelligence_prediction,
    handle_get_intelligence_insights,
    handle_get_value_radar,
    handle_get_hot_matches_v2,
    handle_get_intelligence_movers,
    handle_get_intelligence_history,
    handle_get_intelligence_performance,
    handle_admin_intelligence_overview,
)

logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT_DIR, "web")
ASSETS_DIR = os.path.join(ROOT_DIR, "assets")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Enable CORS headers for Telegram Mini App webview clients."""
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, Authorization"
    response.headers["Access-Control-Max-Age"] = "86400"

    # Static assets (JS/CSS) in Telegram WebView: must revalidate so updates land immediately
    if request.path.startswith(("/js/", "/css/", "/static/")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    # Логотипы, аватары и фото игроков меняются редко: сутки берём из кэша WebView,
    # не спрашивая сервер. Ошибки (404 ещё не залитого логотипа) не кэшируем.
    elif request.path.startswith("/assets/") and response.status in (200, 304):
        response.headers["Cache-Control"] = "public, max-age=86400"

    return response


# Сжимаем то, что сжимается: JS/CSS/HTML фронтенда и JSON API. PNG/JPEG уже сжаты.
_COMPRESSIBLE_STATIC_EXT = (".js", ".css", ".html", ".json", ".svg", ".txt")
_MIN_COMPRESS_BYTES = 1024


def _should_compress(request: web.Request, response: web.StreamResponse) -> bool:
    if response.status != 200 or request.method == "HEAD":
        return False
    if "gzip" not in request.headers.get(hdrs.ACCEPT_ENCODING, "").lower():
        return False
    # Сжатие и Range несовместимы; уже сжатый ответ второй раз не жмём.
    if hdrs.RANGE in request.headers or hdrs.CONTENT_ENCODING in response.headers:
        return False

    path = request.path
    if isinstance(response, web.FileResponse):
        return path in ("/", "/app") or path.lower().endswith(_COMPRESSIBLE_STATIC_EXT)
    if isinstance(response, web.Response):
        body = response.body
        return (
            response.content_type == "application/json"
            and isinstance(body, (bytes, bytearray))
            and len(body) >= _MIN_COMPRESS_BYTES
        )
    return False


@web.middleware
async def compression_middleware(request: web.Request, handler):
    """gzip для текстовых ответов: по мобильной сети JS/CSS Mini App грузятся в разы быстрее."""
    response = await handler(request)
    if _should_compress(request, response):
        response.enable_compression(web.ContentCoding.gzip)
        response.headers[hdrs.VARY] = hdrs.ACCEPT_ENCODING
    return response


def _rate_limit_response(error_code: str, retry_after: int) -> web.Response:
    messages = {
        "rate_limit_exceeded": "Слишком много запросов. Подождите немного.",
        "too_fast": "Слишком часто. Подождите пару секунд перед повтором.",
        "duplicate_request": "Предыдущий запрос ещё обрабатывается. Подождите результата.",
    }
    return web.json_response(
        {
            "status": "error",
            "error": error_code,
            "message": messages.get(error_code, messages["rate_limit_exceeded"]),
            "retry_after": retry_after,
        },
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """
    Ограничение частоты обращений к REST API Mini App.

    Порядок намеренно такой: сначала окно запросов в минуту, затем минимальный
    интервал для денежных мутаций, затем захват in-flight слота. Слот
    освобождается в finally, иначе упавший обработчик заблокировал бы
    пользователю этот маршрут навсегда.
    """
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return await handler(request)

    from config import API_RATE_LIMIT_ENABLED
    if not API_RATE_LIMIT_ENABLED:
        return await handler(request)

    from api import rate_limiter

    allowed, error_code, retry_after = rate_limiter.check_request(request)
    if not allowed:
        logger.warning(
            "RATE_LIMIT %s: %s %s retry_after=%s",
            error_code, request.method, request.path, retry_after
        )
        return _rate_limit_response(error_code, retry_after)

    dedup_key = rate_limiter.in_flight_key(request)
    if dedup_key is None:
        return await handler(request)

    if not rate_limiter.acquire_in_flight(dedup_key):
        logger.warning("RATE_LIMIT duplicate_request: %s %s", request.method, request.path)
        return _rate_limit_response("duplicate_request", 1)

    try:
        return await handler(request)
    finally:
        rate_limiter.release_in_flight(dedup_key)


@web.middleware
async def lockdown_middleware(request: web.Request, handler):
    """
    Global Server-Side Lockdown Middleware for REST API endpoints.
    Enforces that during LOGOVO_LOCKDOWN, only Global Admins can access API resources.
    Follows strict authentication order:
    1. Validate Telegram initData (returns 401 if invalid/missing).
    2. Extract user_id.
    3. Check Global Admin privilege.
    4. If not Global Admin during lockdown -> returns 403 LOGOVO_LOCKDOWN.
    5. If Global Admin -> proceeds to normal handler.
    """
    if request.method == "OPTIONS":
        return await handler(request)

    if not request.path.startswith("/api/"):
        return await handler(request)

    # Мобильный трекер авторизуется Bearer-токеном сессии, а не initData, поэтому
    # проверка ниже отбивала бы его безусловным 401. Тот же гейт доступа он
    # проходит внутри себя: api/routes_tracker.py::_require_session вызывает
    # check_user_access на каждом запросе, так же FAIL-CLOSED.
    if request.path.startswith("/api/tracker/"):
        return await handler(request)

    from config import is_global_lockdown_enabled
    if not is_global_lockdown_enabled():
        return await handler(request)

    from api.auth import extract_init_data, get_authenticated_user
    from handlers.base import is_global_admin

    user_info = get_authenticated_user(extract_init_data(request))
    if not user_info or "id" not in user_info:
        return web.json_response(
            {"status": "error", "error": "unauthorized", "message": "Недействительные данные авторизации Telegram."},
            status=401
        )

    user_id = user_info["id"]
    if not is_global_admin(user_id):
        return web.json_response(
            {
                "error": "LOGOVO_LOCKDOWN",
                "message": "Logovo.bet временно закрыт для пользователей"
            },
            status=403
        )

    return await handler(request)


async def handle_index(request: web.Request) -> web.FileResponse:
    """Serve SPA index.html with anti-caching headers."""
    index_path = os.path.join(WEB_DIR, "index.html")
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return web.FileResponse(index_path, headers=headers)



def create_app() -> web.Application:
    """Construct and configure the aiohttp Application."""
    # cors остаётся снаружи, чтобы 429 и 401 тоже уходили с CORS-заголовками.
    app = web.Application(middlewares=[cors_middleware, compression_middleware, rate_limit_middleware, lockdown_middleware])

    # 1. Wallet & Bootstrap
    app.router.add_get("/api/bootstrap", handle_bootstrap)
    app.router.add_get("/api/wallet", handle_get_wallet)
    app.router.add_post("/api/bonus/claim", handle_claim_bonus)
    app.router.add_get("/api/leaderboard/division/{division_id}", handle_get_division_leaderboard)

    # 2. Markets & Odds
    app.router.add_get("/api/markets/tours", handle_get_tours)
    app.router.add_get("/api/matches/{id}/markets", handle_get_match_markets)
    app.router.add_get("/api/markets/{id}/odds-history", handle_get_odds_history)

    # 2b. Общий кубок: этапы, сетка и линия этапа (ставки — через /api/predictions)
    app.router.add_get("/api/cup", handle_get_cup)
    app.router.add_get("/api/cup/stages/{id}/bracket", handle_get_cup_bracket)
    app.router.add_get("/api/cup/stages/{id}/line", handle_get_cup_line)

    # 3. Match Center 3.0
    app.router.add_get("/api/matches", handle_get_matches)
    app.router.add_get("/api/matches/hot", handle_get_hot_matches)
    app.router.add_get("/api/recommendations", handle_get_recommendations)
    app.router.add_get("/api/matches/{id}", handle_get_match_detail)
    app.router.add_get("/api/matches/{id}/photo", handle_get_match_photo)
    app.router.add_get("/api/matches/{id}/stats", handle_get_match_stats)
    app.router.add_get("/api/matches/{id}/h2h", handle_get_match_h2h)
    app.router.add_get("/api/matches/{id}/insights", handle_get_match_insights)
    app.router.add_get("/api/matches/{id}/live", handle_get_match_live)

    # 4. Predictions & Coupon Engine
    app.router.add_post("/api/predictions", handle_place_prediction)
    app.router.add_get("/api/predictions", handle_get_predictions)
    app.router.add_get("/api/predictions/{id}", handle_get_prediction_detail)
    app.router.add_post("/api/predictions/{id}/repeat", handle_repeat_prediction)
    app.router.add_get("/api/predictions/{id}/cashout-quote", handle_get_cashout_quote)
    app.router.add_post("/api/predictions/{id}/cashout", handle_execute_cashout)

    # Aliases for bets
    app.router.add_post("/api/bets", handle_place_prediction)
    app.router.add_get("/api/bets", handle_get_predictions)
    app.router.add_get("/api/bets/{id}", handle_get_prediction_detail)

    # 5. Tournament Hub & Divisions & Seasons
    app.router.add_get("/api/divisions", handle_get_divisions)
    app.router.add_get("/api/seasons", handle_get_seasons)
    app.router.add_get("/api/seasons/{id}", handle_get_season_by_id)
    app.router.add_get("/api/standings", handle_get_standings)
    app.router.add_get("/api/table", handle_get_standings)
    app.router.add_get("/api/results", handle_get_results)
    app.router.add_get("/api/tournaments", handle_get_tournaments)
    app.router.add_get("/api/tournaments/{id}/standings", handle_get_standings)
    app.router.add_get("/api/tournaments/{id}/results", handle_get_results)
    app.router.add_get("/api/tournaments/{id}/top-scorers", handle_get_top_scorers)
    app.router.add_get("/api/profile/tournament-stats", handle_get_my_tournament_stats)

    # 6. User Stats, Saved Coupons, Favorites & Notifications
    app.router.add_get("/api/stats/me", handle_get_my_stats)
    app.router.add_get("/api/profile/analytics", handle_get_profile_analytics)
    app.router.add_post("/api/saved-coupons", handle_save_coupon)
    app.router.add_get("/api/saved-coupons", handle_get_saved_coupons)
    app.router.add_delete("/api/saved-coupons/{id}", handle_delete_saved_coupon)
    app.router.add_post("/api/favorites", handle_add_favorite)
    app.router.add_get("/api/favorites", handle_get_favorites)
    app.router.add_delete("/api/favorites/{id}", handle_delete_favorite)
    app.router.add_get("/api/notifications", handle_get_notifications)
    app.router.add_post("/api/notifications/read", handle_mark_notifications_read)

    # 7. Phase 10: Gamification, Profile 2.0, Fair Leaderboard & Seasonal Progression
    app.router.add_get("/api/progression", handle_get_progression)
    app.router.add_get("/api/achievements", handle_get_achievements)
    app.router.add_post("/api/achievements/claim", handle_claim_achievement)
    app.router.add_get("/api/profile", handle_get_profile)
    app.router.add_get("/api/profile/stats", handle_get_profile_stats)
    app.router.add_get("/api/profile/{user_id}", handle_get_profile)
    app.router.add_get("/api/player/{id}/public", handle_get_player_public)
    app.router.add_get("/api/leaderboard", handle_get_leaderboard)
    app.router.add_get("/api/leaderboard/division", handle_get_leaderboard_division)
    app.router.add_get("/api/leaderboard/season", handle_get_leaderboard_season)
    app.router.add_get("/api/season", handle_get_season)
    app.router.add_get("/api/season/rewards", handle_get_season_rewards)

    # Admin Season Center
    app.router.add_get("/api/admin/season", handle_admin_get_season)
    app.router.add_post("/api/admin/season", handle_admin_create_season)
    app.router.add_post("/api/admin/season/finalize", handle_admin_finalize_season)
    app.router.add_post("/api/admin/season/rewards", handle_admin_season_rewards)

    # 8. Phase 5: Admin Betting Center
    app.router.add_get("/api/admin/markets", handle_admin_list_markets)
    app.router.add_post("/api/admin/markets/{id}/transition", handle_admin_transition_market)
    app.router.add_put("/api/admin/markets/{id}/odds", handle_admin_update_odds)
    app.router.add_get("/api/admin/bets", handle_admin_list_bets)
    app.router.add_post("/api/admin/bets/{id}/void", handle_admin_void_bet)
    app.router.add_get("/api/admin/audit-log", handle_admin_audit_log)

    # 9. Phase 6: Live Match Center & Odds Movers
    app.router.add_get("/api/live", handle_get_live_matches)
    app.router.add_get("/api/live/{id}", handle_get_live_match_detail)
    app.router.add_get("/api/live/{id}/events", handle_get_live_events)
    app.router.add_get("/api/live/{id}/stats", handle_get_live_stats)
    app.router.add_get("/api/live/{id}/markets", handle_get_live_markets)
    app.router.add_get("/api/live/{id}/intelligence", handle_get_live_intelligence)
    app.router.add_get("/api/odds/movers", handle_get_odds_movers)

    # Phase 6: Admin Live Center & Safety Controls
    app.router.add_get("/api/admin/live/overview", handle_admin_live_overview)
    app.router.add_post("/api/admin/live/markets/{id}/suspend", handle_admin_suspend_market)
    app.router.add_post("/api/admin/live/markets/{id}/resume", handle_admin_resume_market)
    app.router.add_post("/api/admin/live/markets/{id}/close", handle_admin_close_market)
    app.router.add_post("/api/admin/live/markets/{id}/void", handle_admin_void_market)
    app.router.add_post("/api/admin/live/matches/{id}/correction", handle_admin_match_correction)
    app.router.add_post("/api/admin/live/matches/{id}/refresh", handle_admin_refresh_match)
    app.router.add_get("/api/admin/sports/health", handle_admin_sports_health)

    # 10. Phase 7: AI & Advanced Sports Intelligence
    app.router.add_get("/api/intelligence/matches", handle_get_intelligence_matches)
    app.router.add_get("/api/intelligence/matches/{id}", handle_get_intelligence_match_detail)
    app.router.add_get("/api/intelligence/matches/{id}/preview", handle_get_intelligence_preview)
    app.router.add_get("/api/intelligence/matches/{id}/prediction", handle_get_intelligence_prediction)
    app.router.add_get("/api/intelligence/matches/{id}/insights", handle_get_intelligence_insights)
    app.router.add_get("/api/intelligence/value", handle_get_value_radar)
    app.router.add_get("/api/intelligence/hot", handle_get_hot_matches_v2)
    app.router.add_get("/api/intelligence/movers", handle_get_intelligence_movers)
    app.router.add_get("/api/intelligence/history", handle_get_intelligence_history)
    app.router.add_get("/api/intelligence/performance", handle_get_intelligence_performance)
    app.router.add_get("/api/admin/intelligence/overview", handle_admin_intelligence_overview)

    # 11. Phase 9: Admin Risk Center & Exposure Controls
    app.router.add_get("/api/admin/risk/exposure", handle_admin_get_exposure)
    app.router.add_get("/api/admin/risk/alerts", handle_admin_get_risk_alerts)
    app.router.add_post("/api/admin/risk/alerts/{id}/ack", handle_admin_ack_alert)
    app.router.add_post("/api/admin/risk/alerts/{id}/resolve", handle_admin_resolve_alert)
    app.router.add_get("/api/admin/risk/limits", handle_admin_get_limits)
    app.router.add_post("/api/admin/risk/limits", handle_admin_set_limits)
    app.router.add_post("/api/admin/risk/suspend", handle_admin_emergency_suspend)

    # 12. Player Cabinet («Мой Клуб»)
    app.router.add_get("/api/cabinet/overview", handle_get_cabinet_overview)
    app.router.add_get("/api/cabinet/matches", handle_get_cabinet_matches)
    app.router.add_get("/api/cabinet/squad", handle_get_cabinet_squad)
    app.router.add_post("/api/cabinet/match-time", handle_post_cabinet_match_time)

    # 13. Logovo Tracker («мобильное приложение live-трансляции»)
    app.router.add_post("/api/tracker/auth/pair", handle_tracker_pair)
    app.router.add_get("/api/tracker/matches", handle_tracker_matches)
    app.router.add_post("/api/tracker/session/start", handle_tracker_session_start)
    app.router.add_post("/api/tracker/session/tick", handle_tracker_session_tick)
    app.router.add_post("/api/tracker/session/event", handle_tracker_session_event)
    app.router.add_post("/api/tracker/session/finish", handle_tracker_session_finish)

    # Static SPA Frontend & Assets
    app.router.add_get("/", handle_index)
    app.router.add_get("/app", handle_index)
    if os.path.exists(WEB_DIR):
        app.router.add_static("/static/", WEB_DIR, show_index=True)
        app.router.add_static("/css/", os.path.join(WEB_DIR, "css"))
        app.router.add_static("/js/", os.path.join(WEB_DIR, "js"))
    if os.path.exists(ASSETS_DIR):
        app.router.add_static("/assets/", ASSETS_DIR, show_index=True)

    return app


async def start_api_server_background(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start embedded API server in background within current asyncio loop."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"🎰 Logovo.bet Mini App Server running at http://{host}:{port}")
    return runner


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web_app = create_app()
    web.run_app(web_app, host="0.0.0.0", port=8080)
