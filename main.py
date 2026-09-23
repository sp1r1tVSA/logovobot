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
    try:
        from api.server import start_api_server_background
        import config
        await start_api_server_background(host=config.API_HOST, port=config.API_PORT)
    except Exception as e:
        logger.warning(f"Failed to start Logovo.bet Mini App server: {e}")

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

    # 🔄 Auto-recalculate line markets on startup with calibrated Poisson engine
    try:
        import asyncio
        from services.betting_engine import regenerate_all_active_markets
        count = await asyncio.to_thread(regenerate_all_active_markets)
        logger.info(f"🎰 Regenerated betting markets for {count} active matches on startup")
    except Exception as e:
        logger.warning(f"Failed to auto-regenerate markets on startup: {e}")

def register_jobs(application: Application) -> None:
    """Register periodic background jobs."""
    # Check round deadlines & send reminders every 30 minutes
    application.job_queue.run_repeating(job_check_deadlines_and_remind, interval=1800, first=30)
    # Post/update debts summary in ПРЕДЫ thread every 12 hours
    application.job_queue.run_repeating(job_post_debts_to_warns, interval=12 * 3600, first=60)
    # Run automated debt lifecycle tracker (reminders + auto-warns + auto-kick) every 30 minutes
    application.job_queue.run_repeating(job_debt_lifecycle_tracker, interval=1800, first=90)

    # Phase 6: Live provider sync, intelligence cache & smart notifications
    try:
        from services.background_sync import (
            sync_live_provider_job,
            sync_intelligence_cache_job,
            process_notification_queue_job,
            settle_finished_bets_job,
        )
        application.job_queue.run_repeating(sync_live_provider_job, interval=45, first=15)
        application.job_queue.run_repeating(sync_intelligence_cache_job, interval=300, first=45)
        # Always on: bet win/refund notices go through this queue. With
        # SMART_NOTIFICATIONS_ENABLED off the job delivers only those.
        application.job_queue.run_repeating(process_notification_queue_job, interval=15, first=20)
        # Bet settlement used to run inline on Mini App requests; now scheduled off the loop.
        application.job_queue.run_repeating(settle_finished_bets_job, interval=60, first=25)
    except Exception as e:
        logger.warning(f"Could not register Phase 6 background jobs: {e}")

    # Round analytics: превью открытого тура и итоги сыгранного в топик АНАЛИТИКА.
    # Отдельный try/except — падение аналитики не должно ронять остальные джобы.
    try:
        from handlers.admin import job_post_round_preview, job_post_round_digest
        application.job_queue.run_repeating(job_post_round_preview, interval=600, first=120)
        application.job_queue.run_repeating(job_post_round_digest, interval=900, first=150)
    except Exception as e:
        logger.warning(f"Could not register round analytics jobs: {e}")

    # Детектор договорных матчей: только считает индекс подозрительности и
    # показывает дела супер-админу, ставки никогда не блокирует.
    try:
        from services.background_sync import scan_integrity_job
        application.job_queue.run_repeating(scan_integrity_job, interval=120, first=60)
    except Exception as e:
        logger.warning(f"Could not register integrity scan job: {e}")

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
