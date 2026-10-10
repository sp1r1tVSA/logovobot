import logging
from telegram.ext import ApplicationBuilder, Application
from config import TOKEN
from database import init_db
from handlers import register_all_handlers, job_check_deadlines_and_remind, job_post_debts_to_warns, job_debt_lifecycle_tracker

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# httpx пишет на INFO полный URL запроса, а токен бота — часть пути Telegram API.
# На INFO это отправляло бы токен в journalctl в каждой строке; поднимаем порог до
# WARNING, чтобы сетевые ошибки было видно, а секрет в логи не попадал.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def post_init(application: Application) -> None:
    # Меню команд: общее для всех и личное для админов (с /overview).
    # Сбой меню не должен мешать старту бота.
    try:
        from handlers.bot_menu import set_default_menu, sync_admin_menus
        await set_default_menu(application.bot)
        applied = await sync_admin_menus(application.bot)
        logger.info(f"Admin command menu set for {applied} admin(s)")
    except Exception as e:
        logger.warning(f"Failed to set bot command menu: {e}")

    # 🎰 Start Logovo.bet Telegram Mini App API server
    from services import job_health
    try:
        from api.server import start_api_server_background
        import config
        from transfers import set_bot as set_transfer_bot
        set_transfer_bot(application.bot)
        await start_api_server_background(host=config.API_HOST, port=config.API_PORT, bot=application.bot)
        job_health.record_component("api_server", True, f"порт {config.API_PORT}")
    except Exception as e:
        logger.warning(f"Failed to start Logovo.bet Mini App server: {e}")
        job_health.record_component("api_server", False, f"{type(e).__name__}: {e}")


    # 📱 Configure Telegram WebApp Menu Button
    try:
        from telegram import MenuButtonWebApp, MenuButtonDefault, WebAppInfo
        import config

        if config.WEBAPP_URL and config.WEBAPP_URL.startswith("https://"):
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🎰 Logovo.bet", web_app=WebAppInfo(url=config.WEBAPP_URL))
            )
        else:
            await application.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as e:
        logger.warning(f"Could not set WebApp menu button: {e}")

    # 🧹 Рынки «Открыт» у матчей с закрытой линией (след старых «ленивых» генераций)
    try:
        import asyncio
        import database
        closed = await asyncio.to_thread(database.close_orphan_open_markets)
        if closed:
            logger.info(f"🧹 Closed {closed} orphan open markets (line closed) on startup")
    except Exception as e:
        logger.warning(f"Failed to close orphan open markets on startup: {e}")

    # 🔄 Auto-recalculate line markets on startup with calibrated Poisson engine
    try:
        import asyncio
        from services.betting_engine import regenerate_all_active_markets
        count = await asyncio.to_thread(regenerate_all_active_markets)
        logger.info(f"🎰 Regenerated betting markets for {count} active matches on startup")
        job_health.record_component("startup_markets", True, f"пересчитано матчей: {count}")
    except Exception as e:
        logger.warning(f"Failed to auto-regenerate markets on startup: {e}")
        job_health.record_component("startup_markets", False, f"{type(e).__name__}: {e}")

def _run_repeating(application: Application, name: str, callback, interval: float, first: float) -> None:
    """Schedule a job wrapped by job_health, so /health sees it and admins get alerted."""
    from services import job_health
    application.job_queue.run_repeating(
        job_health.tracked(name, callback, interval), interval=interval, first=first, name=name,
    )


def register_jobs(application: Application) -> None:
    """Register periodic background jobs."""
    # Check round deadlines & send reminders every 30 minutes
    _run_repeating(application, "deadline_reminders", job_check_deadlines_and_remind, 1800, 30)
    # Post/update debts summary in ПРЕДЫ thread every 12 hours
    _run_repeating(application, "debts_digest", job_post_debts_to_warns, 12 * 3600, 60)
    # Run automated debt lifecycle tracker (reminders + auto-warns + auto-kick) every 30 minutes
    _run_repeating(application, "debt_lifecycle", job_debt_lifecycle_tracker, 1800, 90)

    # Phase 6: Live provider sync, intelligence cache & smart notifications
    try:
        from services.background_sync import (
            sync_live_provider_job,
            sync_intelligence_cache_job,
            process_notification_queue_job,
            settle_finished_bets_job,
        )
        _run_repeating(application, "live_provider_sync", sync_live_provider_job, 45, 15)
        _run_repeating(application, "intelligence_cache", sync_intelligence_cache_job, 300, 45)
        # Always on: bet win/refund notices go through this queue. With
        # SMART_NOTIFICATIONS_ENABLED off the job delivers only those.
        _run_repeating(application, "notification_queue", process_notification_queue_job, 15, 20)
        # Bet settlement used to run inline on Mini App requests; now scheduled off the loop.
        _run_repeating(application, "bet_settlement", settle_finished_bets_job, 60, 25)
    except Exception as e:
        logger.warning(f"Could not register Phase 6 background jobs: {e}")

    # Round analytics: превью открытого тура и итоги сыгранного в топик АНАЛИТИКА.
    # Отдельный try/except — падение аналитики не должно ронять остальные джобы.
    try:
        from handlers.admin import job_post_round_preview, job_post_round_digest
        _run_repeating(application, "round_preview", job_post_round_preview, 600, 120)
        _run_repeating(application, "round_digest", job_post_round_digest, 900, 150)
    except Exception as e:
        logger.warning(f"Could not register round analytics jobs: {e}")

    # Символическая сборная: раз на каждый полностью сыгранный блок из 5 туров.
    try:
        from handlers.admin import job_post_totw, job_check_first_half_completion
        _run_repeating(application, "totw", job_post_totw, 900, 180)
        _run_repeating(application, "first_half_check", job_check_first_half_completion, 900, 200)
    except Exception as e:
        logger.warning(f"Could not register TOTW / first-half jobs: {e}")

    # Детектор договорных матчей: только считает индекс подозрительности и
    # показывает дела супер-админу, ставки никогда не блокирует.
    try:
        from services.background_sync import scan_integrity_job
        _run_repeating(application, "integrity_scan", scan_integrity_job, 120, 60)
    except Exception as e:
        logger.warning(f"Could not register integrity scan job: {e}")

    # Долгосрочные рынки: пересчёт цен после подтверждённых матчей и авторасчёт.
    # Пересчитываются только рынки, чьё состояние изменилось; Монте-Карло — в потоке.
    try:
        from services.outright_service import REFRESH_INTERVAL_SECONDS, refresh_outrights_job
        _run_repeating(application, "outrights_refresh", refresh_outrights_job, REFRESH_INTERVAL_SECONDS, 75)
    except Exception as e:
        logger.warning(f"Could not register outright markets job: {e}")

    # Бэкап базы: джоба проверяет раз в 30 минут, пора ли (по возрасту последнего
    # файла), так что рестарт не пропускает и не удваивает копию.
    try:
        import config
        if config.BACKUP_INTERVAL_HOURS > 0:
            from handlers.admin_ops import job_auto_backup
            _run_repeating(application, "db_backup", job_auto_backup, 1800, 240)
        else:
            logger.info("Auto backup disabled (BACKUP_INTERVAL_HOURS=0)")
    except Exception as e:
        logger.warning(f"Could not register backup job: {e}")

    # Трансферное окно: автозакрытие по времени из настроек окна
    try:
        from transfers.jobs import job_auto_close
        _run_repeating(application, "transfer_auto_close", job_auto_close, 60, 100)
    except Exception as e:
        logger.warning(f"Could not register transfer auto-close job: {e}")

    # Трансферное окно: напоминания тренерам и ответственному (за сутки/час до закрытия, зависшие заявки)
    try:
        from transfers.jobs import job_reminders
        _run_repeating(application, "transfer_reminders", job_reminders, 300, 120)
    except Exception as e:
        logger.warning(f"Could not register transfer reminders job: {e}")

    # Ставки на реальные матчи: выбор матча дня, кэфы букмекера, расчёт по счёту провайдера.
    try:
        import config
        if config.IRL_ENABLED:
            from services.irl_jobs import job_irl_pick, job_irl_odds_refresh, job_irl_settle
            _run_repeating(application, "irl_pick", job_irl_pick, 600, 110)
            _run_repeating(application, "irl_odds_refresh", job_irl_odds_refresh, 1800, 130)
            _run_repeating(application, "irl_settle", job_irl_settle, 300, 160)
        else:
            logger.info("IRL betting disabled (IRL_ENABLED=false)")
    except Exception as e:
        logger.warning(f"Could not register IRL betting jobs: {e}")

def main() -> None:
    """Initialize and run the Telegram bot application."""
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN or BOT_TOKEN found in environment variables!")
        print("Error: Please set TELEGRAM_BOT_TOKEN in your .env file.")
        return

    # Initialize the database
    try:
        init_db()
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        return

    # Build the Telegram Application
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Register all handlers (modular registration)
    register_all_handlers(application)

    # Register periodic background jobs
    register_jobs(application)

    # Start the bot
    logger.info("Starting Telegram bot...")
    try:
        application.run_polling()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")

if __name__ == "__main__":
    main()
