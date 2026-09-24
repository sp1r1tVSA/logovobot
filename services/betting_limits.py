"""
services/betting_limits.py

Logovo.bet — Centralized Betting & Exposure Limits Service.
Single authoritative source of truth for:
- Stake limits (MIN_BET, MAX_BET)
- Payout caps (MAX_PAYOUT)
- User limits (MAX_DAILY_STAKE, MAX_DAILY_LOSS, MAX_OPEN_EXPOSURE, MAX_OPEN_BETS)
- Exposure caps (MARKET_EXPOSURE_LIMIT, DIVISION_EXPOSURE_LIMIT, GLOBAL_EXPOSURE_LIMIT)

Server-authoritative: Used across API, Mini App, Handlers, and Risk Engine.
Zero hidden or hardcoded limits.
"""

import logging
from typing import Any, Optional
import database

logger = logging.getLogger(__name__)

# Default Global Constants (Coins 🪙)
DEFAULT_MIN_BET: int = 10
DEFAULT_MAX_BET: int = 50_000
DEFAULT_MAX_PAYOUT: int = 10_000
DEFAULT_MAX_DAILY_STAKE: int = 100_000
DEFAULT_MAX_DAILY_LOSS: int = 50_000
DEFAULT_MAX_OPEN_EXPOSURE: int = 35_000
# Сколько купонов игрок может держать открытыми одновременно. Экспресс из пяти
# матчей — это один купон: ограничиваем число пари, а не число исходов.
DEFAULT_MAX_OPEN_BETS: int = 12
DEFAULT_MARKET_EXPOSURE_LIMIT: int = 250_000
DEFAULT_DIVISION_EXPOSURE_LIMIT: int = 1_000_000
DEFAULT_GLOBAL_EXPOSURE_LIMIT: int = 5_000_000
# Не лимиты риска, а настройки экономики и купона: хранятся в той же таблице и
# меняются из той же панели, но читает их database (get_max_express_events,
# get_express_margin_pct, get_initial_wallet_balance), а не RiskEngine.
DEFAULT_MAX_EXPRESS_EVENTS: int = database.MAX_EXPRESS_EVENTS
DEFAULT_EXPRESS_MARGIN_PCT: int = database.EXPRESS_MARGIN_PCT
DEFAULT_INITIAL_BALANCE: int = database.INITIAL_WALLET_BALANCE


class BettingLimitsService:
    """Centralized limit resolution with hierarchical override support (User -> Division -> Global)."""

    @staticmethod
    def get_default_limits() -> dict[str, int]:
        """Значения по умолчанию — то, к чему вернётся сброшенный глобальный лимит."""
        return {
            "min_bet": DEFAULT_MIN_BET,
            "max_bet": DEFAULT_MAX_BET,
            "max_payout": DEFAULT_MAX_PAYOUT,
            "max_daily_stake": DEFAULT_MAX_DAILY_STAKE,
            "max_daily_loss": DEFAULT_MAX_DAILY_LOSS,
            "max_open_exposure": DEFAULT_MAX_OPEN_EXPOSURE,
            "max_open_bets": DEFAULT_MAX_OPEN_BETS,
            "market_exposure_limit": DEFAULT_MARKET_EXPOSURE_LIMIT,
            "division_exposure_limit": DEFAULT_DIVISION_EXPOSURE_LIMIT,
            "global_exposure_limit": DEFAULT_GLOBAL_EXPOSURE_LIMIT,
            "max_express_events": DEFAULT_MAX_EXPRESS_EVENTS,
            "express_margin_pct": DEFAULT_EXPRESS_MARGIN_PCT,
            "initial_balance": DEFAULT_INITIAL_BALANCE,
        }

    @classmethod
    def get_system_limits(cls) -> dict[str, int]:
        """Return canonical baseline system limits."""
        return {
            "min_bet": cls.get_limit("global", 0, "min_bet", DEFAULT_MIN_BET),
            "max_bet": cls.get_limit("global", 0, "max_bet", DEFAULT_MAX_BET),
            "max_payout": cls.get_limit("global", 0, "max_payout", DEFAULT_MAX_PAYOUT),
            "max_daily_stake": cls.get_limit("global", 0, "max_daily_stake", DEFAULT_MAX_DAILY_STAKE),
            "max_daily_loss": cls.get_limit("global", 0, "max_daily_loss", DEFAULT_MAX_DAILY_LOSS),
            "max_open_exposure": cls.get_limit("global", 0, "max_open_exposure", DEFAULT_MAX_OPEN_EXPOSURE),
            "max_open_bets": cls.get_limit("global", 0, "max_open_bets", DEFAULT_MAX_OPEN_BETS),
            "market_exposure_limit": cls.get_limit("global", 0, "market_exposure_limit", DEFAULT_MARKET_EXPOSURE_LIMIT),
            "division_exposure_limit": cls.get_limit("global", 0, "division_exposure_limit", DEFAULT_DIVISION_EXPOSURE_LIMIT),
            "global_exposure_limit": cls.get_limit("global", 0, "global_exposure_limit", DEFAULT_GLOBAL_EXPOSURE_LIMIT),
        }

    @classmethod
    def get_division_limits(cls, division_id: int) -> dict[str, int]:
        """Return effective limits for a specific division."""
        sys_limits = cls.get_system_limits()
        div_max_bet = cls.get_limit("division", division_id, "max_bet", sys_limits["max_bet"])
        div_max_payout = cls.get_limit("division", division_id, "max_payout", sys_limits["max_payout"])
        div_market_limit = cls.get_limit("division", division_id, "market_exposure_limit", sys_limits["market_exposure_limit"])
        div_exposure_limit = cls.get_limit("division", division_id, "division_exposure_limit", sys_limits["division_exposure_limit"])
        # Само число открытых купонов считается по игроку целиком, а не по дивизиону;
        # здесь настраивается только потолок — чтобы его можно было поднять одной лиге.
        div_open_bets = cls.get_limit("division", division_id, "max_open_bets", sys_limits["max_open_bets"])

        return {
            **sys_limits,
            "division_id": division_id,
            "max_bet": div_max_bet,
            "max_payout": div_max_payout,
            "max_open_bets": div_open_bets,
            "market_exposure_limit": div_market_limit,
            "division_exposure_limit": div_exposure_limit,
        }

    @classmethod
    def get_user_effective_limits(cls, user_id: int, division_id: Optional[int] = None) -> dict[str, int]:
        """
        Return the most restrictive applicable limits for a user:
        Considers wallet.daily_limit, custom user limits, division limits, and global system limits.
        """
        base = cls.get_division_limits(division_id) if division_id else cls.get_system_limits()

        # Check user wallet daily_limit override
        try:
            wallet = database.get_or_create_wallet(user_id)
            user_daily_limit = wallet.get("daily_limit")
            if user_daily_limit and user_daily_limit > 0:
                base["max_daily_stake"] = min(base["max_daily_stake"], int(user_daily_limit))
        except Exception as e:
            logger.debug(f"Could not load wallet daily_limit for user #{user_id}: {e}")

        # Check custom user limits
        user_max_bet = cls.get_limit("user", user_id, "max_bet", base["max_bet"])
        base["max_bet"] = min(base["max_bet"], user_max_bet)

        user_daily_stake = cls.get_limit("user", user_id, "max_daily_stake", base["max_daily_stake"])
        base["max_daily_stake"] = min(base["max_daily_stake"], user_daily_stake)

        user_daily_loss = cls.get_limit("user", user_id, "max_daily_loss", base["max_daily_loss"])
        base["max_daily_loss"] = min(base["max_daily_loss"], user_daily_loss)

        user_open_exposure = cls.get_limit("user", user_id, "max_open_exposure", base["max_open_exposure"])
        base["max_open_exposure"] = min(base["max_open_exposure"], user_open_exposure)

        user_max_payout = cls.get_limit("user", user_id, "max_payout", base["max_payout"])
        base["max_payout"] = min(base["max_payout"], user_max_payout)

        user_open_bets = cls.get_limit("user", user_id, "max_open_bets", base["max_open_bets"])
        base["max_open_bets"] = min(base["max_open_bets"], user_open_bets)

        return base

    @staticmethod
    def get_limit(scope_type: str, scope_id: int, limit_key: str, default_value: int) -> int:
        """Fetch limit from database config or fallback to default."""
        try:
            with database.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT limit_value FROM risk_limits_config
                    WHERE scope_type = ? AND scope_id = ? AND limit_key = ?
                """, (scope_type, scope_id, limit_key))
                row = cursor.fetchone()
                if row and row["limit_value"] is not None:
                    return int(row["limit_value"])
        except Exception as e:
            logger.warning(f"Error fetching risk limit ({scope_type}:{scope_id}:{limit_key}): {e}")
        return default_value

    @classmethod
    def get_global_limits(cls) -> dict[str, int]:
        """Alias for get_system_limits."""
        return cls.get_system_limits()

    @classmethod
    def set_division_limits(cls, division_id: int, limits: dict[str, int], updated_by: int = 0) -> bool:
        """Batch set limits for a division."""
        for k, v in limits.items():
            cls.set_limit("division", division_id, k, int(v))
        return True

    @classmethod
    def set_user_limits(cls, user_id: int, limits: dict[str, int], updated_by: int = 0) -> bool:
        """Batch set limits for a user."""
        for k, v in limits.items():
            cls.set_limit("user", user_id, k, int(v))
        return True

    @classmethod
    def set_global_limits(cls, limits: dict[str, int], updated_by: int = 0) -> bool:
        """Batch set global system limits."""
        for k, v in limits.items():
            cls.set_limit("global", 0, k, int(v))
        return True

    @staticmethod
    def set_limit(scope_type: str, scope_id: int, limit_key: str, limit_value: int) -> bool:
        """Persist or update a limit in risk_limits_config."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO risk_limits_config (scope_type, scope_id, limit_key, limit_value, updated_at)
                VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))
                ON CONFLICT(scope_type, scope_id, limit_key)
                DO UPDATE SET limit_value = excluded.limit_value, updated_at = datetime('now', '+3 hours')
            """, (scope_type, scope_id, limit_key, limit_value))
            return True
