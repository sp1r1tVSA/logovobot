from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    TypeHandler,
    ApplicationHandlerStop,
    filters,
    ContextTypes,
)
import asyncio
import database
import logging
import telegram.error
from handlers.base import is_global_admin, is_logovo_access_allowed

from handlers.chat import handle_ai_chat
from handlers.squad_ai import squad_ai_apply

# Import base handlers
from handlers.base import (
    start,
    show_main_menu,
    show_divisions_list,
    show_division_menu,
    show_division_table,
    show_division_scorers,
    show_division_assists,
    show_division_totw_menu,
    show_division_totw,
    show_support,
    group_table_command,
    show_round_matches,
    cb_refresh_division_table_topic,
)

# Import cabinet handlers
from handlers.cabinet import (
    show_cabinet,
    show_club_stats,
    show_my_squad,
    show_my_matches_stub,
    show_game_history_stub,
    show_edit_profile_menu,
    start_registration,
    reg_team_name,
    start_selective_edit,
    save_selective_edit,
    show_my_matches,
    cabinet_view_match,
    cabinet_view_squad,
    show_game_history,
    cancel_registration,
    TEAM_NAME,
    EDITING_FIELD,
    start_score_reporting,
    cb_report_choice_auto,
    cb_report_choice_manual,
    cb_confirm_ai_final,
    cb_cup_winner,
    cb_report_home_goals,
    cb_report_away_goals,
    cb_pick_goal,
    cb_skip_goals,
    cb_pick_assist,
    cb_skip_assists,
    cb_mvp_team,
    cb_mvp_pick,
    cb_mvp_skip,
    cb_mvp_back,
    prompt_photo_upload,
    save_report_photo,
    ai_recognize_now,
    submit_report_to_guest,
    cb_guest_confirm,
    cb_guest_reject,
    cb_skip_report_photo,
    REPORT_SCORE_PHOTO,
    SQUAD_PHOTO,
    MATCH_CUSTOM_TIME,
    start_upload_squad,
    start_upload_reserves,
    save_squad_photo,
    cancel_upload_squad,
    start_custom_time_prompt,
    save_custom_match_time,
    cb_propose_time_prompt,
    cb_quick_time,
    cb_accept_time,
    cb_request_admin_result,
    cb_admin_approve_result,
    cancel_score_report_and_navigate,
    show_player_card,
    show_my_club_card,
    show_specific_club_card,
    show_club_graphic_card,
    show_club_squad,
    show_club_history,
    show_clubs_catalog,
    show_clubs_catalog_divisions,
    show_clubs_catalog_for_division,
    club_command,
)

# Import admin handlers
from handlers.admin import (
    show_admin_panel,
    show_super_admin_panel,
    show_division_admin_panel,
    admin_div_admins_hub,
    admin_div_admins_view,
    admin_div_admin_remove,
    admin_div_admin_add_start,
    admin_div_admin_add_receive,
    admin_div_admin_cancel,
    ADMIN_EXPECT_DIV_ADMIN_REF,
    admin_div_manage_matches,
    admin_div_round,
    admin_div_round_matches,
    admin_div_broadcast_debts,
    admin_div_debts_menu,
    admin_div_debts_dm,
    admin_div_manage_players,
    admin_toggle_chat_mode,
    admin_toggle_ai_chat,
    admin_list_players,
    admin_gen_div_select,
    admin_generate_matches_execute,
    admin_manage_players_info,
    admin_list_players_page,
    admin_view_player,
    admin_confirm_delete_player,
    admin_delete_player_execute,
    admin_toggle_round_bets,
    admin_open_preseason_line,
    admin_extend_match_execute,
    admin_extend_menu,
    admin_extend_hours_execute,
    admin_list_overdue,
    admin_open_round_prompt,
    admin_open_round_save,
    ADMIN_WAITING_FOR_DEADLINE,
    admin_open_batch_prompt,
    admin_open_batch_deadline,
    ADMIN_WAITING_FOR_BATCH_DEADLINE,
    admin_close_round,
    admin_close_round_confirm,
admin_round_matches,
    admin_view_match,
    admin_view_match_photo,
    admin_report_score_auto,
    admin_set_technical_result_execute,
    admin_reset_match_execute,
    admin_add_player_start,
    admin_add_player_username,
    admin_add_player_div_callback,
    admin_add_player_club_callback,
    admin_add_player_manual_club_callback,
    admin_add_player_manual_club_text,
    ADMIN_EXPECT_PLAYER_USERNAME,
    ADMIN_EXPECT_PLAYER_DIVISION,
    ADMIN_EXPECT_PLAYER_CLUB,
    ADMIN_EXPECT_MANUAL_CLUB,
    admin_import_players_start,
    admin_import_players_text,
    ADMIN_EXPECT_IMPORT_TEXT,
    admin_edit_club_start,
    admin_edit_club_text,
    ADMIN_EXPECT_NEW_CLUB,
    admin_set_score_start,
    admin_set_score_text,
    ADMIN_EXPECT_MATCH_SCORE,
    admin_cancel_player_action,
    admin_cancel_match_action,
    admin_toggle_role,
    admin_delete_options,
    admin_confirm_wipe_player,
    admin_wipe_player_execute,
    admin_edit_username_start,
    admin_edit_username_text,
    ADMIN_EXPECT_NEW_USERNAME,
    admin_clear_league_start,
    admin_clear_league_text,
    ADMIN_EXPECT_RESET_CONFIRM,
    admin_manage_players_menu,
    admin_edit_club_select,
    admin_edit_club_execute,
    admin_bind_hub,
    admin_bind_division,
    admin_bind_club_card,
    admin_bind_execute,
    admin_bind_free_confirm,
    admin_bind_free_execute,
    admin_edit_div_select,
    admin_edit_div_execute,
    admin_div_players_menu,
    admin_list_div_players,
    admin_divs_hub,
    admin_div_view,
    admin_div_toggle,
    admin_div_topics_menu,
    admin_div_create_start,
    admin_div_create_receive,
    admin_div_rename_start,
    admin_div_rename_receive,
    admin_div_settopic_prompt,
    admin_div_settopic_receive,
    admin_cancel_div_action,
    admin_set_div_topic_cmd,
    ADMIN_EXPECT_DIV_NAME,
    ADMIN_EXPECT_DIV_RENAME,
    ADMIN_EXPECT_DIV_TOPIC_ID,
    admin_delete_player_confirm,
    admin_remind_round,
    admin_toggle_remind_match,
    admin_toggle_remind_all,
    admin_send_selected_reminders,
    job_check_deadlines_and_remind,
    job_post_debts_to_warns,
    job_debt_lifecycle_tracker,
    admin_set_squad_topic,
    admin_set_drafts_topic,
    admin_set_reports_topic,
    admin_set_results_topic,
    admin_set_warns_topic,
    admin_rosters_for_division,
    admin_view_squad,
    admin_squad_rm_menu,
    admin_squad_del_player,
    admin_squad_upload_start,
    admin_squad_upload_text,
    admin_squad_upload_photo,
    admin_squad_add_player_start,
    admin_squad_add_player_text,
    admin_squad_clear,
    admin_squad_add_missing,
    ADMIN_EXPECT_SQUAD_TEXT,
    ADMIN_EXPECT_SINGLE_PLAYER,
    admin_stub,
    admin_fetch_photos,
    admin_force_update,
    admin_test_ai,
    admin_warn_confirm,
    admin_warn_execute,
    admin_warn_remove_execute,
    admin_warn_history,
    admin_amnesty_execute,
    admin_reset_season_warns,
    admin_reset_debts_command,
    admin_check_debts_command,
    admin_unwarn_command,
    admin_round_preview_command,
    admin_round_digest_command,
    admin_totw_post_command,
    cb_totw_publish,
    admin_squads_status_command,
    admin_squads_view_cb,
    admin_squads_all_cb,
    admin_squads_remind_cb,
)

from handlers.topic_management import (
    register_topic_management_handlers,
    cmd_bind_group,
    cb_bind_group,
)
from handlers.cup_management import register_cup_handlers
from handlers.league_overview import register_league_overview_handlers
from handlers.admin_bets import (
    cmd_admin_bets,
    cb_admin_bets_navigate,
    cb_admin_bets_toggle_alerts,
    cb_admin_bet_detail,
    cb_admin_bet_void_ask,
    cb_admin_bet_void_execute,
    cmd_admin_integrity,
    cb_admin_integrity_navigate,
    cb_admin_integrity_case,
    cb_admin_integrity_review,
)
from services.topic_cache import topic_cache


logger = logging.getLogger(__name__)

LOCKDOWN_BOT_MESSAGE = "🔒 <b>Logovo.bet временно закрыт.</b>\n\nДоступ разрешён только администраторам."
LOCKDOWN_ALERT_MESSAGE = "🔒 Logovo.bet временно закрыт.\n\nДоступ разрешён только администраторам."

async def global_lockdown_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    High-priority guard (group -1) that stops all non-admin interactions during LOGOVO_LOCKDOWN.
    """
    from config import is_global_lockdown_enabled
    if not is_global_lockdown_enabled():
        return

    user = update.effective_user
    user_id = user.id if user else None

    # Global admins bypass lockdown completely
    if user_id and is_global_admin(user_id):
        return

    # User is not a global admin -> BLOCK!
    if update.callback_query:
        try:
            await update.callback_query.answer(
                LOCKDOWN_ALERT_MESSAGE,
                show_alert=True
            )
        except Exception as e:
            logger.debug(f"Failed to answer callback query in lockdown guard: {e}")
        raise ApplicationHandlerStop()

    if update.effective_message:
        msg = update.effective_message
        chat = update.effective_chat
        is_private = bool(chat and chat.type == "private")
        is_command = bool(msg.text and msg.text.startswith("/"))
        bot_username = (context.bot.username or "").lower() if context.bot else ""
        is_bot_mention = bool(
            msg.text and bot_username and f"@{bot_username}" in msg.text.lower()
        )
        is_reply_to_bot = bool(
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.is_bot
            and (not bot_username or (msg.reply_to_message.from_user.username or "").lower() == bot_username)
        )

        if is_private or is_command or is_bot_mention or is_reply_to_bot:
            try:
                await msg.reply_text(LOCKDOWN_BOT_MESSAGE, parse_mode="HTML")
            except Exception as e:
                logger.debug(f"Failed to reply lockdown text: {e}")

        raise ApplicationHandlerStop()

    # Any other update type from non-admin during lockdown is halted
    raise ApplicationHandlerStop()


async def handle_placeholders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if query.data == "noop":
        await query.answer()
        return
    await query.answer("Эта функция находится в разработке.", show_alert=True)

async def track_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track the ID of the Telegram group the bot is in."""
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        await asyncio.to_thread(database.set_config, "group_id", str(update.effective_chat.id))

    # Collect style samples from a persona source user (e.g. @t3miy) for AI learning
    try:
        if update.effective_user and update.message and update.message.text:
            source_username = database.get_config("style_source_username") or "t3miy"
            msg_username = (update.effective_user.username or "").lower()
            if msg_username and msg_username == source_username.lower():
                await asyncio.to_thread(database.append_style_sample, update.message.text)
                await asyncio.to_thread(database.trim_style_samples, keep=100)
    except Exception as e:
        logger.debug(f"Failed to collect style sample: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error that occurred during update handling."""
    if not context.error:
        return
    err_str = str(context.error).lower()
    if "query is too old" in err_str or "message is not modified" in err_str:
        logger.debug(f"Telegram ошибка, игнорируется: {context.error}")
        return
    if isinstance(context.error, (telegram.error.TimedOut, telegram.error.NetworkError)):
        logger.warning(f"Сетевая задержка Telegram (TimedOut/NetworkError): {context.error}")
        return
    if isinstance(context.error, telegram.error.BadRequest):
        logger.debug(f"Telegram BadRequest (игнорируется): {context.error}")
        return
    logger.error(f"Исключение при обработке обновления: {context.error}", exc_info=context.error)

def _register_user_handlers(app: Application) -> None:
    """Register general user command and navigation handlers."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("table", group_table_command))
    app.add_handler(CommandHandler("ratings", group_table_command))
    app.add_handler(CommandHandler("fetch_photos", admin_fetch_photos))

    app.add_handler(MessageHandler(filters.Regex("^👤 Мой кабинет$"), show_cabinet))
    app.add_handler(MessageHandler(filters.Regex("^💬 Поддержка$"), show_support))
    
    from handlers.drafts import handle_draft_media, cb_draft_confirm, cb_draft_reject
    app.add_handler(MessageHandler((filters.PHOTO | filters.TEXT) & filters.ChatType.GROUPS & ~filters.COMMAND, handle_draft_media), group=2)
    app.add_handler(CallbackQueryHandler(cb_draft_confirm, pattern="^draft_conf_"))
    app.add_handler(CallbackQueryHandler(cb_draft_reject, pattern="^draft_rej_"))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Админ-панель$"), show_admin_panel))

    app.add_handler(CallbackQueryHandler(cb_refresh_division_table_topic, pattern=r"^refresh_div_table_\d+$"))
    app.add_handler(CallbackQueryHandler(show_cabinet, pattern="^menu_cabinet$"))
    app.add_handler(CallbackQueryHandler(show_divisions_list, pattern="^(menu_divisions|menu_league)$"))
    app.add_handler(CallbackQueryHandler(show_division_menu, pattern=r"^division_view:(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_division_table, pattern=r"^division_table:(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_division_scorers, pattern=r"^division_scorers:(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_division_assists, pattern=r"^division_assists:(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_division_totw_menu, pattern=r"^division_totw:(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_division_totw, pattern=r"^division_totw_view:(\d+):(\d+):(\d+):(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_support, pattern="^menu_support$"))
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern="^main_menu$"))
    app.add_handler(CommandHandler("club", club_command))

    # Logovo Tracker: /tracker и /app выдают ПИН для мобильного приложения.
    from handlers.tracker import tracker_command
    app.add_handler(CommandHandler(["tracker", "app"], tracker_command))

    # Вызов тренера и управление плашками клубов
    from handlers.text_commands import cmd_summon_club, cmd_sync_club_titles, cmd_totw
    app.add_handler(CommandHandler(["summon", "call", "pozvat"], cmd_summon_club))
    app.add_handler(CommandHandler(["totw", "sbornaya"], cmd_totw))
    app.add_handler(CommandHandler(["set_club_titles", "sync_titles"], cmd_sync_club_titles))

    app.add_handler(CallbackQueryHandler(show_my_club_card, pattern="^cb_my_club_card$"))
    app.add_handler(CallbackQueryHandler(show_clubs_catalog, pattern="^cb_clubs_catalog$"))
    app.add_handler(CallbackQueryHandler(show_clubs_catalog_for_division, pattern=r"^clubs_catalog_div:(\d+)$"))
    app.add_handler(CallbackQueryHandler(show_specific_club_card, pattern="^view_club_.+$"))
    app.add_handler(CallbackQueryHandler(show_club_graphic_card, pattern="^img_club_.+$"))
    app.add_handler(CallbackQueryHandler(show_club_squad, pattern="^clsquad_.+$"))
    app.add_handler(CallbackQueryHandler(show_club_history, pattern="^clhist_.+$"))
    app.add_handler(CallbackQueryHandler(show_round_matches, pattern="^show_round_matches_\\d+$"))

def _register_cabinet_handlers(app: Application) -> None:
    """Register player cabinet FSM and interactive match handlers."""
    reg_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_registration, pattern="^register_profile$"),
            CallbackQueryHandler(start_selective_edit, pattern="^edit_field_.*$"),
            CommandHandler("register", start_registration)
        ],
        states={
            TEAM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_team_name)],
            EDITING_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_selective_edit)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_registration),
            MessageHandler(filters.Regex("^Отмена$"), cancel_registration)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(reg_conv)

    score_report_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_score_reporting, pattern="^cabinet_report_score_\\d+$")
        ],
        states={
            REPORT_SCORE_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.ALL, save_report_photo)]
        },
        fallbacks=[
            CallbackQueryHandler(cabinet_view_match, pattern="^cabinet_view_match_\\d+$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^cabinet_my_matches$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^main_menu$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^menu_cabinet$"),
            CommandHandler("cancel", cancel_registration)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(score_report_conv)

    squad_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_upload_squad, pattern="^cabinet_upload_squad$"),
            CallbackQueryHandler(start_upload_reserves, pattern="^cabinet_upload_reserves$"),
        ],
        states={
            SQUAD_PHOTO: [MessageHandler(filters.PHOTO, save_squad_photo)]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_upload_squad, pattern="^cabinet_my_squad$"),
            CommandHandler("cancel", cancel_upload_squad)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(squad_conv)

    custom_time_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_custom_time_prompt, pattern="^cb_custom_time_prompt_\\d+$")
        ],
        states={
            MATCH_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_custom_match_time)]
        },
        fallbacks=[
            CallbackQueryHandler(cabinet_view_match, pattern="^cabinet_view_match_\\d+$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^cabinet_my_matches$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^main_menu$"),
            CallbackQueryHandler(cancel_score_report_and_navigate, pattern="^menu_cabinet$"),
            CommandHandler("cancel", cancel_registration)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(custom_time_conv)

    app.add_handler(CallbackQueryHandler(show_edit_profile_menu, pattern="^edit_profile_menu$"))
    app.add_handler(CallbackQueryHandler(show_my_matches, pattern="^cabinet_my_matches$"))
    app.add_handler(CallbackQueryHandler(cabinet_view_match, pattern="^cabinet_view_match_\\d+$"))
    app.add_handler(CallbackQueryHandler(cabinet_view_squad, pattern="^cabinet_view_squad_\\d+$"))
    app.add_handler(CallbackQueryHandler(show_game_history, pattern="^cabinet_game_history$"))

    app.add_handler(CallbackQueryHandler(cb_propose_time_prompt, pattern="^cb_propose_time_prompt_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_quick_time, pattern="^cb_quick_time_\\d+_.+$"))
    app.add_handler(CallbackQueryHandler(cb_accept_time, pattern="^cb_accept_time_\\d+$"))

    app.add_handler(CallbackQueryHandler(cb_request_admin_result, pattern="^cb_request_admin_result_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_approve_result, pattern="^cb_admin_approve_\\d+_\\d+$"))

    app.add_handler(CallbackQueryHandler(cb_report_choice_auto, pattern="^cb_report_choice_auto_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_report_choice_manual, pattern="^cb_report_choice_manual_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_confirm_ai_final, pattern="^cb_confirm_ai_final_\\d+$"))
    # Шаг кубковой приёмки результата («кто прошёл дальше») обязан стоять до
    # catch-all группы 0: иначе нажатие утонуло бы в AI-чате.
    app.add_handler(CallbackQueryHandler(cb_cup_winner, pattern="^cb_cup_winner_\\d+_[12]$"))
    app.add_handler(CallbackQueryHandler(cb_report_home_goals, pattern="^cb_report_hg_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_report_away_goals, pattern="^cb_report_ag_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_pick_goal, pattern="^cb_pick_goal_idx_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_skip_goals, pattern="^cb_skip_goals$"))
    app.add_handler(CallbackQueryHandler(cb_pick_assist, pattern="^cb_pick_assist_idx_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_skip_assists, pattern="^cb_skip_assists$"))
    app.add_handler(CallbackQueryHandler(cb_mvp_team, pattern="^cb_mvp_team_(home|away)$"))
    app.add_handler(CallbackQueryHandler(cb_mvp_pick, pattern="^cb_mvp_(pick|squad)_idx_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_mvp_skip, pattern="^cb_mvp_skip$"))
    app.add_handler(CallbackQueryHandler(cb_mvp_back, pattern="^cb_mvp_back$"))
    app.add_handler(CallbackQueryHandler(submit_report_to_guest, pattern="^cb_submit_report_to_guest(_\\d+)?$"))
    # Opponent confirmation is gone; these two only defuse buttons still sitting
    # in players' chats from before the change.
    app.add_handler(CallbackQueryHandler(cb_guest_confirm, pattern="^cb_guest_confirm_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_guest_reject, pattern="^cb_guest_reject_\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_skip_report_photo, pattern="^cb_skip_report_photo$"))
    app.add_handler(CallbackQueryHandler(ai_recognize_now, pattern="^ai_recognize_now_\\d+$"))

    async def global_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or update.effective_chat.type != "private":
            return
        if context.user_data.get("awaiting_report_photo") or context.user_data.get("reporting_match_id"):
            await save_report_photo(update, context)

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL), global_photo_handler))

    app.add_handler(CallbackQueryHandler(show_club_stats, pattern="^cabinet_club_stats$"))
    app.add_handler(CallbackQueryHandler(show_my_squad, pattern="^cabinet_my_squad$"))
    app.add_handler(CallbackQueryHandler(show_player_card, pattern="^(player_card|pcard)_.+$"))

def _register_admin_handlers(app: Application) -> None:
    """Register administrator panel, tournament management, and dispute resolution handlers."""
    admin_player_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_player_start, pattern="^admin_add_player_start$"),
            CallbackQueryHandler(admin_import_players_start, pattern="^admin_import_players_start$"),
            CommandHandler("add_player", admin_add_player_start),
            CommandHandler("import_players", admin_import_players_start),
            CallbackQueryHandler(admin_edit_club_start, pattern="^admin_edit_club_start_-?\\d+$"),
            CallbackQueryHandler(admin_edit_username_start, pattern="^admin_edit_username_start_\\d+$"),
            CallbackQueryHandler(admin_clear_league_start, pattern="^admin_clear_league_start$")
        ],
        states={
            ADMIN_EXPECT_PLAYER_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_player_username)],
            ADMIN_EXPECT_PLAYER_DIVISION: [CallbackQueryHandler(admin_add_player_div_callback, pattern="^admin_add_player_div_\\d+$")],
            ADMIN_EXPECT_PLAYER_CLUB: [
                CallbackQueryHandler(admin_add_player_manual_club_callback, pattern="^admin_add_player_manual_club$"),
                CallbackQueryHandler(admin_add_player_club_callback, pattern="^assign_club_.*$")
            ],
            ADMIN_EXPECT_MANUAL_CLUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_player_manual_club_text)],
            ADMIN_EXPECT_IMPORT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_import_players_text)],
            ADMIN_EXPECT_NEW_CLUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_club_text)],
            ADMIN_EXPECT_NEW_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_username_text)],
            ADMIN_EXPECT_RESET_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_clear_league_text)]
        },
        fallbacks=[
            CallbackQueryHandler(admin_cancel_player_action, pattern="^admin_cancel_player_action$"),
            CommandHandler("cancel", admin_cancel_player_action)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(admin_player_conv)

    admin_match_score_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_set_score_start, pattern="^admin_set_score_start_\\d+$")
        ],
        states={
            ADMIN_EXPECT_MATCH_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_score_text)]
        },
        fallbacks=[
            CallbackQueryHandler(admin_cancel_match_action, pattern="^admin_cancel_match_action$"),
            CommandHandler("cancel", admin_cancel_match_action)
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(admin_match_score_conv)

    admin_round_deadline_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_open_round_prompt, pattern=r"^admin_div_round_open:\d+:\d+$")
        ],
        states={
            ADMIN_WAITING_FOR_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_open_round_save)]
        },
        fallbacks=[
            CallbackQueryHandler(admin_cancel_match_action, pattern="^admin_cancel_match_action$"),
            CommandHandler("cancel", admin_cancel_match_action),
            MessageHandler(filters.Regex("^(Отмена|отмена)$"), admin_cancel_match_action) # <-- Добавлено!
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(admin_round_deadline_conv)

    admin_batch_round_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_open_batch_prompt, pattern=r"^admin_batch_open_div:\d+$")
        ],
        states={
            ADMIN_WAITING_FOR_BATCH_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_open_batch_deadline)]
        },
        fallbacks=[
            CallbackQueryHandler(admin_cancel_match_action, pattern="^admin_cancel_match_action$"),
            CommandHandler("cancel", admin_cancel_match_action),
            MessageHandler(filters.Regex("^(Отмена|отмена)$"), admin_cancel_match_action) # <-- Добавлено!
        ],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300
    )
    app.add_handler(admin_batch_round_conv)

    admin_squad_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_squad_upload_start, pattern="^admin_squad_upload_.*$"),
            CallbackQueryHandler(admin_squad_add_player_start, pattern="^admin_squad_add_player_.*$")
        ],
        states={
            ADMIN_EXPECT_SQUAD_TEXT: [
                MessageHandler(filters.PHOTO, admin_squad_upload_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_squad_upload_text),
            ],
            ADMIN_EXPECT_SINGLE_PLAYER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_squad_add_player_text)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_view_squad, pattern="^admin_squad_view_.*$"),
            CommandHandler("cancel", admin_cancel_player_action)
        ],
        allow_reentry=True,
        per_message=False,
        per_user=True,
        conversation_timeout=300
    )
    app.add_handler(admin_squad_conv)

    admin_div_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_div_create_start, pattern="^admin_div_create_start$"),
            CallbackQueryHandler(admin_div_rename_start, pattern="^admin_div_rename_\\d+$"),
            # Must stay a literal: tests/test_production_audit.py scrapes patterns
            # from the source text. Kept in sync with database.PRIMARY_DIVISION_TOPICS
            # by tests/test_division_topic_coverage.py. "drafts" and "tables" are no
            # longer offered as buttons but stay accepted, so buttons in already-sent
            # messages keep working.
            CallbackQueryHandler(admin_div_settopic_prompt, pattern="^admin_div_settopic_\\d+_(draft|drafts|previews|results|reports|lineups|analytics|tables)$"),
        ],
        states={
            ADMIN_EXPECT_DIV_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_div_create_receive)],
            ADMIN_EXPECT_DIV_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_div_rename_receive)],
            ADMIN_EXPECT_DIV_TOPIC_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_div_settopic_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_divs_hub, pattern="^admin_divs_hub$"),
            CallbackQueryHandler(admin_div_view, pattern="^admin_div_view_\\d+$"),
            CommandHandler("cancel", admin_cancel_div_action),
            MessageHandler(filters.Regex("^(Отмена|отмена)$"), admin_cancel_div_action),
        ],
        allow_reentry=True,
        per_message=False,
        per_user=True,
        conversation_timeout=300
    )
    app.add_handler(admin_div_conv)

    # RBAC: назначение админа дивизиона (супер-админ)
    admin_div_admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_div_admin_add_start, pattern="^admin_div_admin_add_\\d+$"),
        ],
        states={
            ADMIN_EXPECT_DIV_ADMIN_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_div_admin_add_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(admin_div_admins_view, pattern="^admin_div_admins_view_\\d+$"),
            CallbackQueryHandler(admin_div_admins_hub, pattern="^admin_div_admins_hub$"),
            CommandHandler("cancel", admin_div_admin_cancel),
        ],
        allow_reentry=True,
        per_message=False,
        per_user=True,
        conversation_timeout=300
    )
    app.add_handler(admin_div_admin_conv)

    app.add_handler(CommandHandler("set_div_topic", admin_set_div_topic_cmd))
    register_topic_management_handlers(app)

    # Общий кубок: панель этапов и тема вещания. До catch-all группы 0 — иначе
    # кнопки утонули бы в AI-чате (ловушка №4 из AGENTS.md).
    register_cup_handlers(app)

    # /overview — сводка по всем дивизионам (туры, долги, варны).
    register_league_overview_handlers(app)

    app.add_handler(CommandHandler("set_squad_topic", admin_set_squad_topic))
    app.add_handler(CommandHandler("set_drafts_topic", admin_set_drafts_topic))
    app.add_handler(CommandHandler("set_reports_topic", admin_set_reports_topic))
    app.add_handler(CommandHandler("set_results_topic", admin_set_results_topic))
    app.add_handler(CommandHandler("set_warns_topic", admin_set_warns_topic))

    app.add_handler(CallbackQueryHandler(show_admin_panel, pattern="^admin_main_menu$"))

    # RBAC: панели и изолированные точки входа админа дивизиона
    app.add_handler(CallbackQueryHandler(show_division_admin_panel, pattern=r"^admin_div_panel:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_manage_matches, pattern=r"^admin_div_manage_matches:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_round, pattern=r"^admin_div_round:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_round_matches, pattern=r"^admin_div_round_matches:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_debts_menu, pattern=r"^admin_div_debts_menu:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_debts_dm, pattern=r"^admin_div_debts_dm:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_broadcast_debts, pattern=r"^admin_div_broadcast_debts:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_manage_players, pattern=r"^(admin_div_manage_players:\d+|admin_div_players:\d+:\d+)$"))

    # RBAC: управление админами дивизионов (супер-админ)
    app.add_handler(CallbackQueryHandler(admin_div_admins_hub, pattern="^admin_div_admins_hub$"))
    app.add_handler(CallbackQueryHandler(admin_div_admins_view, pattern="^admin_div_admins_view_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_admin_remove, pattern="^admin_div_admin_del_\\d+_-?\\d+$"))

    app.add_handler(CallbackQueryHandler(admin_divs_hub, pattern="^admin_divs_hub$"))
    app.add_handler(CallbackQueryHandler(admin_div_view, pattern="^admin_div_view_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_toggle, pattern="^admin_div_toggle_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_div_topics_menu, pattern="^admin_div_topics_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_chat_mode, pattern="^admin_toggle_chat_mode$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_ai_chat, pattern="^admin_toggle_ai_chat$"))
    app.add_handler(CallbackQueryHandler(admin_gen_div_select, pattern=r"^admin_gen_div_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_generate_matches_execute, pattern=r"^admin_gen_exec_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_manage_players_menu, pattern="^admin_manage_players$"))
    app.add_handler(CallbackQueryHandler(admin_manage_players_menu, pattern="^admin_manage_players_info$"))
    app.add_handler(CallbackQueryHandler(admin_list_players_page, pattern="^admin_list_players_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_view_player, pattern="^admin_view_player_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_club_select, pattern="^admin_edit_club_select_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_club_execute, pattern="^admin_eclub_-?\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_div_select, pattern="^admin_edit_div_select_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_edit_div_execute, pattern="^admin_ediv_-?\\d+_(\\d+|none)$"))
    app.add_handler(CallbackQueryHandler(admin_div_players_menu, pattern="^admin_div_players_menu$"))
    app.add_handler(CallbackQueryHandler(admin_list_div_players, pattern="^admin_list_div_players_"))
    # Two separate confirm screens with near-identical names: each parses its own
    # prefix out of callback_data, so they must not share a pattern.
    app.add_handler(CallbackQueryHandler(admin_confirm_delete_player, pattern="^admin_confirm_delete_player_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_player_confirm, pattern="^admin_delete_player_confirm_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_player_execute, pattern="^admin_delete_player_execute_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_round_bets, pattern=r"^admin_div_bets_(open|close):\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_open_preseason_line, pattern=r"^admin_div_preseason_line:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_close_round, pattern=r"^admin_div_round_close:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_close_round_confirm, pattern=r"^admin_div_round_close_ok:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_remind_round, pattern="^admin_remind_round_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_remind_match, pattern="^admin_toggle_remind_match_\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_remind_all, pattern="^admin_toggle_remind_all_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_send_selected_reminders, pattern="^admin_send_selected_reminders_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_round_matches, pattern="^admin_round_matches_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_view_match, pattern="^admin_view_match_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_view_match_photo, pattern="^admin_view_match_photo_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_reset_match_execute, pattern="^admin_reset_match_execute_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_report_score_auto, pattern="^admin_report_score_auto_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_set_technical_result_execute, pattern="^admin_tp_(home|away|draw)_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_list_overdue, pattern=r"^admin_div_overdue:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_extend_match_execute, pattern="^admin_extend_match_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_extend_menu, pattern="^admin_extend_menu_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_extend_hours_execute, pattern="^admin_extend_(24|48)h_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_role, pattern="^admin_toggle_role_-?\\d+_(player|admin)$"))
    app.add_handler(CallbackQueryHandler(admin_delete_options, pattern="^admin_delete_options_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_confirm_wipe_player, pattern="^admin_confirm_wipe_player_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_wipe_player_execute, pattern="^admin_wipe_player_execute_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_rosters_for_division, pattern=r"^admin_roster_div:(\d+)$"))
    # Привязка клубов. Клуб едет индексом (кириллица не влезает в 64 байта callback_data),
    # а telegram_id допускает минус — преднабранные игроки живут с отрицательными id.
    app.add_handler(CallbackQueryHandler(admin_bind_hub, pattern=r"^admin_bind_hub$"))
    app.add_handler(CallbackQueryHandler(admin_bind_division, pattern=r"^admin_bind_div:\d+(:h)?$"))
    app.add_handler(CallbackQueryHandler(admin_bind_club_card, pattern=r"^admin_bind_club:\d+:\d+:\d+(:h)?$"))
    app.add_handler(CallbackQueryHandler(admin_bind_execute, pattern=r"^admin_bind_set:\d+:\d+:-?\d+(:h)?$"))
    app.add_handler(CallbackQueryHandler(admin_bind_free_confirm, pattern=r"^admin_bind_free:\d+:\d+(:h)?$"))
    app.add_handler(CallbackQueryHandler(admin_bind_free_execute, pattern=r"^admin_bind_free_ok:\d+:\d+(:h)?$"))
    app.add_handler(CallbackQueryHandler(admin_view_squad, pattern="^admin_squad_view_.*$"))
    app.add_handler(CallbackQueryHandler(admin_squad_rm_menu, pattern="^admin_squad_rm_menu_.*$"))
    app.add_handler(CallbackQueryHandler(admin_squad_del_player, pattern="^admin_squad_del_p_.*$"))
    app.add_handler(CallbackQueryHandler(admin_squad_clear, pattern="^admin_squad_clear_.*$"))
    app.add_handler(CallbackQueryHandler(admin_squad_add_missing, pattern="^admin_squad_add_missing_.*$"))
    app.add_handler(CallbackQueryHandler(squad_ai_apply, pattern="^squadai_(add|replace|cancel)$"))
    app.add_handler(CommandHandler("force_update", admin_force_update))
    app.add_handler(CallbackQueryHandler(admin_force_update, pattern="^admin_force_update$"))
    app.add_handler(CommandHandler("test_ai", admin_test_ai))
    app.add_handler(CallbackQueryHandler(admin_fetch_photos, pattern="^admin_fetch_photos_cb$"))
    app.add_handler(CallbackQueryHandler(admin_stub, pattern="^admin_matches_stub$"))

    # Warns system
    app.add_handler(CallbackQueryHandler(admin_warn_confirm, pattern="^warn_add_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_warn_execute, pattern="^warn_exec_-?\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_warn_remove_execute, pattern="^warn_remove_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_warn_history, pattern="^warn_hist_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_amnesty_execute, pattern="^warn_amnesty_-?\\d+$"))
    app.add_handler(CallbackQueryHandler(admin_reset_season_warns, pattern="^admin_reset_season_warns$"))
    app.add_handler(CommandHandler("reset_debts", admin_reset_debts_command))
    app.add_handler(CommandHandler("reset_warns", admin_reset_debts_command))
    app.add_handler(CommandHandler("clear_warns", admin_reset_debts_command))
    app.add_handler(CommandHandler("unwarn", admin_unwarn_command))
    app.add_handler(CommandHandler("check_debts", admin_check_debts_command))
    app.add_handler(CommandHandler("debug_debts", admin_check_debts_command))
    # Ручной прогон автопостинга в топик АНАЛИТИКА (обычно этим занимаются джобы)
    app.add_handler(CommandHandler("round_preview", admin_round_preview_command))
    app.add_handler(CommandHandler("round_digest", admin_round_digest_command))
    # Символическая сборная: ручная публикация (обычно — джоба job_post_totw)
    app.add_handler(CommandHandler("totw_post", admin_totw_post_command))
    app.add_handler(CallbackQueryHandler(cb_totw_publish, pattern=r"^totw_publish:\d+:\d+:\d+$"))

    # Squads status
    app.add_handler(CommandHandler(["squads_status", "squads", "sostavy"], admin_squads_status_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/(составы|состав)(?:@\w+)?(?:\s+.*)?$"), admin_squads_status_command))
    app.add_handler(CallbackQueryHandler(admin_squads_view_cb, pattern=r"^admin_squads_view:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_squads_all_cb, pattern=r"^admin_squads_all$"))
    app.add_handler(CallbackQueryHandler(admin_squads_remind_cb, pattern=r"^admin_squads_remind:\d+$"))

    # 🎰 Super-Admin Bets Monitoring & Tracking (Private Chat Only)
    app.add_handler(CommandHandler(["admin_bets", "all_bets", "track_bets"], cmd_admin_bets))
    # CommandHandler принимает только [a-z0-9_] — кириллический алиас ловим regex-ом, как /составы
    app.add_handler(MessageHandler(filters.Regex(r"^/ставки_админ(?:@\w+)?(?:\s+.*)?$"), cmd_admin_bets))
    app.add_handler(CallbackQueryHandler(cmd_admin_bets, pattern="^admin_bets_hub$"))
    app.add_handler(CallbackQueryHandler(cb_admin_bets_navigate, pattern=r"^admin_bets_(page|flt|refresh):"))
    app.add_handler(CallbackQueryHandler(cb_admin_bets_toggle_alerts, pattern=r"^admin_bets_alerts_toggle:"))
    app.add_handler(CallbackQueryHandler(cb_admin_bet_detail, pattern=r"^admin_bet_view:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_bet_void_ask, pattern=r"^admin_bet_void_ask:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_admin_bet_void_execute, pattern=r"^admin_bet_void_do:\d+$"))

    # 🕵️ Детектор договорных матчей — тот же приватный супер-админский контур
    app.add_handler(CommandHandler(["integrity", "suspicions"], cmd_admin_integrity))
    app.add_handler(CallbackQueryHandler(cmd_admin_integrity, pattern="^admin_integrity_hub$"))
    app.add_handler(CallbackQueryHandler(cb_admin_integrity_navigate, pattern=r"^admin_integrity_(page|flt|refresh):"))
    app.add_handler(CallbackQueryHandler(cb_admin_integrity_case, pattern=r"^admin_integrity_case:\d+"))
    app.add_handler(CallbackQueryHandler(cb_admin_integrity_review, pattern=r"^admin_integrity_(ack|dismiss|confirm):\d+"))

def register_all_handlers(application: Application) -> None:
    """Register all command, message, and callback handlers to the application."""
    # 0. Global lockdown guard at group -1 (runs before all standard handlers)
    application.add_handler(TypeHandler(Update, global_lockdown_guard), group=-1)

    application.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_id), group=1)
    
    # 1. Сначала регистрируем кнопки и основные команды
    _register_user_handlers(application)
    
    # 2. Затем диалоги кабинета и админки (FSM conversation handlers)
    _register_cabinet_handlers(application)
    _register_admin_handlers(application)

    # 🎰 Logovo.bet: Virtual Prediction & Sportsbook Handlers
    from handlers.betting import register_betting_handlers
    register_betting_handlers(application)

    # 3. И ТОЛЬКО В САМОМ КОНЦЕ перехватчик текста и голосовых сообщений для ИИ Темшика!
    application.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, handle_ai_chat))

    # Final catch-all for inline button clicks in development
    application.add_handler(CallbackQueryHandler(handle_placeholders, pattern=".*"))

    # Initialize TopicCache for multi-division forum topics routing
    topic_cache.reload_cache()

    # Register global error handler
    application.add_error_handler(error_handler)