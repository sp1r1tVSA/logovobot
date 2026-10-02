"""Чистый движок трансферного окна: деньги, бюджет клуба, проверки заявок."""

import json

import pytest

from transfers.engine import (
    ClubLedger,
    RequestContext,
    TransferRequest,
    WindowSettings,
    compute_ledger,
    core_remaining,
    evaluate_request,
    format_k,
    is_full_latin_name,
    parse_money_k,
    surcharge_cost,
    urn_payout,
)

OPEN = WindowSettings(status="open")


def _codes(issues):
    return {i.code for i in issues}


def _t(id_, kind, status, price_k=0, from_club=None, to_club=None):
    return {"id": id_, "kind": kind, "status": status, "price_k": price_k,
            "from_club": from_club, "to_club": to_club}


class TestMoney:
    @pytest.mark.parametrize("raw, expected", [
        ("12.5", 12500), ("12,5", 12500), ("12.5 млн", 12500), (10, 10000),
        ("0", 0), (" 7,25 ", 7250), ("0.1", 100), ("3m", 3000),
    ])
    def test_parse(self, raw, expected):
        assert parse_money_k(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", "-1", "1e400", "nan", True])
    def test_parse_rejects(self, raw):
        assert parse_money_k(raw) is None

    def test_format(self):
        assert format_k(12500) == "12.5 млн"
        assert format_k(10000) == "10 млн"
        assert format_k(7250) == "7.25 млн"
        assert format_k(-1500) == "-1.5 млн"
        assert format_k(None) == "—"

    def test_urn_payout_floors_to_100k(self):
        # (25 + 5.35) / 2 = 15.175 → 15.1
        assert urn_payout(25000, 5350, sellable=True) == 15100
        # (25 + 5) / 3 = 10.0
        assert urn_payout(25000, 5000, sellable=False) == 10000
        assert urn_payout(10000, 0, sellable=True, divisor_sellable=4) == 2500

    def test_urn_payout_bad_divisor(self):
        with pytest.raises(ValueError):
            urn_payout(1000, 0, True, divisor_sellable=0)

    def test_surcharge_cost(self):
        table = {100: 10000, 111: 160000}
        assert surcharge_cost(100, table) == 10000
        assert surcharge_cost(105, table) is None
        assert surcharge_cost(None, table) is None


class TestNames:
    @pytest.mark.parametrize("name", ["Kylian Mbappé", "Martin Ødegaard", "Jean-Philippe Mateta",
                                      "N'Golo Kanté"])
    def test_full_latin(self, name):
        assert is_full_latin_name(name)

    @pytest.mark.parametrize("name", ["Mbappé", "Килиан Мбаппе", "K1lian Mbappe", "", None,
                                      "Kylian Мбаппе"])
    def test_not_full_latin(self, name):
        assert not is_full_latin_name(name)

    def test_core_remaining(self):
        snapshot = ["A One", "B Two", "C Three", "D Four"]
        squad = ["a one", "B Two", "C Three", "New Guy"]          # D Four уже ушёл
        assert core_remaining(snapshot, squad, outgoing=["C Three"]) == 2


class TestSettings:
    def test_from_row_parses_json(self):
        row = {"status": "open", "max_buys": 4, "fa_forbidden_clubs": '["Ливерпуль"]',
               "surcharge_table": '{"101": 20000, "bad": 1}', "urn_divisor_sellable": None}
        s = WindowSettings.from_row(row)
        assert s.max_buys == 4
        assert s.fa_forbidden_clubs == ("Ливерпуль",)
        assert dict(s.surcharge_table) == {101: 20000}
        assert s.urn_divisor_sellable == 2      # NULL → значение по умолчанию

    def test_from_row_broken_json(self):
        s = WindowSettings.from_row({"fa_restricted_clubs": "{oops", "surcharge_table": None})
        assert s.fa_restricted_clubs == ()
        assert 100 in s.surcharge_table


class TestLedger:
    def test_spend_counts_pending_income_only_approved(self):
        rows = [
            _t(1, "deal", "approved", 10000, from_club="Арсенал", to_club="Ливерпуль"),
            _t(2, "deal", "pending_manager", 5000, from_club="Челси", to_club="Ливерпуль"),
            _t(3, "deal", "rejected", 99000, from_club="Челси", to_club="Ливерпуль"),
            _t(4, "deal", "approved", 8000, from_club="Ливерпуль", to_club="Челси"),
            _t(5, "deal", "pending_counterparty", 4000, from_club="Ливерпуль", to_club="Арсенал"),
            _t(6, "surcharge", "approved", 10000, to_club="Ливерпуль"),
            _t(7, "urn_sale", "approved", 3000, from_club="Ливерпуль"),
        ]
        led = compute_ledger("ливерпуль", 50000, rows, OPEN)
        assert led.spent_k == 25000
        assert led.earned_k == 11000            # 8 + 3, но не 4 из pending
        assert led.remaining_k == 36000
        assert led.buys_used == 2               # доплата слот не занимает
        assert led.sells_used == 3              # сделки + урна
        assert led.urn_sales == 1

    def test_missing_budget_is_zero(self):
        led = compute_ledger("Ливерпуль", None, [], OPEN)
        assert led.budget_k == 0 and led.remaining_k == 0

    def test_slot_purchases_and_exclusion(self):
        rows = [_t(1, "deal", "pending_manager", 1000, from_club="Челси", to_club="Ливерпуль")]
        purchases = [
            {"club_name": "Ливерпуль", "slot_type": "buy", "status": "active"},
            {"club_name": "Ливерпуль", "slot_type": "sell", "status": "refunded"},
            {"club_name": "Челси", "slot_type": "buy", "status": "active"},
        ]
        led = compute_ledger("Ливерпуль", 0, rows, OPEN, purchases, exclude_transfer_id=1)
        assert led.buys_used == 0 and led.spent_k == 0
        assert led.buys_limit == OPEN.max_buys + 1
        assert led.sells_limit == OPEN.max_sells


def _ledger(club="Ливерпуль", budget=100000, **kw):
    return ClubLedger(club=club, budget_k=budget, **kw)


def _deal(**kw):
    base = dict(kind="deal", player_name="Bukayo Saka", from_club="Арсенал",
                to_club="Ливерпуль", price_k=20000, ovr=108)
    base.update(kw)
    return TransferRequest(**base)


class TestDeal:
    def test_clean_deal(self):
        ctx = RequestContext(settings=OPEN, buyer=_ledger(), seller=_ledger("Арсенал"),
                             seller_core_after=6, player_in_seller_squad=True)
        ev = evaluate_request(_deal(), ctx)
        assert ev.ok and not ev.warnings and ev.price_k == 20000

    def test_window_states(self):
        ctx = RequestContext(settings=WindowSettings(status="draft"))
        assert _codes(evaluate_request(_deal(), ctx).blocks) == {"WINDOW_DRAFT"}
        ctx = RequestContext(settings=WindowSettings(status="closed"))
        assert _codes(evaluate_request(_deal(), ctx).blocks) == {"WINDOW_CLOSED"}

    def test_warnings_do_not_block(self):
        ctx = RequestContext(settings=OPEN, buyer=_ledger(budget=5000, buys_used=3),
                             seller=_ledger("Арсенал", sells_used=3), player_in_seller_squad=False)
        ev = evaluate_request(_deal(), ctx)
        assert ev.ok
        assert _codes(ev.warnings) == {"BUDGET_EXCEEDED", "BUY_LIMIT", "SELL_LIMIT", "NOT_IN_SQUAD"}
        assert json.loads(ev.warnings_json())[0]["code"] == "BUDGET_EXCEEDED"

    def test_hard_blocks(self):
        ctx = RequestContext(settings=OPEN, sanctioned=True, seller_core_after=4)
        ev = evaluate_request(_deal(ovr=114), ctx)
        assert _codes(ev.blocks) == {"SANCTIONED", "OVR_CAP", "CORE_RULE"}

    def test_settings_drive_rules(self):
        s = WindowSettings(status="open", ovr_cap=120, min_core_players=3)
        ev = evaluate_request(_deal(ovr=114), RequestContext(settings=s, seller_core_after=3))
        assert ev.ok

    @pytest.mark.parametrize("kw, code", [
        (dict(to_club="арсенал"), "INVALID_CLUBS"),
        (dict(from_club=None), "INVALID_CLUBS"),
        (dict(price_k=None), "INVALID_PRICE"),
        (dict(ovr=None), "INVALID_OVR"),
        (dict(player_name="  "), "INVALID_PLAYER"),
        (dict(kind="loan"), "INVALID_KIND"),
    ])
    def test_invalid(self, kw, code):
        assert code in _codes(evaluate_request(_deal(**kw), RequestContext(settings=OPEN)).blocks)

    def test_banned_player_is_a_warning(self):
        ctx = RequestContext(settings=OPEN, directory={"banned": 1, "ban_reason": "ходовой", "ovr": 108})
        ev = evaluate_request(_deal(), ctx)
        assert ev.ok and "BANNED_PLAYER" in _codes(ev.warnings)


class TestFreeAgent:
    S = WindowSettings(status="open", fa_opens_at="2026-10-05 12:00:00",
                       fa_forbidden_clubs=("Барселона",), fa_restricted_clubs=("Ливерпуль",))

    def _fa(self, **kw):
        base = dict(kind="free_agent", player_name="Lamine Yamal", from_club="Бетис",
                    to_club="Челси", price_k=3000, ovr=105,
                    commented_at="2026-10-05 12:00:30", reported_budget_k=7000)
        base.update(kw)
        return TransferRequest(**base)

    def test_clean(self):
        ctx = RequestContext(settings=self.S, buyer=_ledger("Челси", budget=10000))
        ev = evaluate_request(self._fa(), ctx)
        assert ev.ok and not ev.warnings

    def test_second_free_agent_blocks(self):
        ctx = RequestContext(settings=self.S, buyer=_ledger("Челси", budget=10000), user_free_agents=1)
        assert _codes(evaluate_request(self._fa(), ctx).blocks) == {"FA_SECOND"}

    def test_all_warnings(self):
        ctx = RequestContext(settings=self.S, buyer=_ledger("Ливерпуль", budget=1000),
                             fa_taken_by={"to_club": "Челси", "commented_at": "2026-10-05 12:00:10"})
        req = self._fa(to_club="Ливерпуль", from_club="барселона", ovr=112,
                       player_name="Ямаль", commented_at="2026-10-05 11:59:59",
                       reported_budget_k=0)
        ev = evaluate_request(req, ctx)
        assert ev.ok
        assert _codes(ev.warnings) == {
            "FA_TOO_EARLY", "FA_FORBIDDEN_CLUB", "FA_RESTRICTED_CLUB", "FA_OVR_CAP",
            "FA_NAME_FORMAT", "FA_TAKEN", "FA_BUDGET_MISMATCH", "BUDGET_EXCEEDED",
        }


class TestSurcharge:
    def _req(self, ovr=111):
        return TransferRequest(kind="surcharge", player_name="Bukayo Saka", to_club="Ливерпуль", ovr=ovr)

    def test_price_from_table(self):
        ctx = RequestContext(settings=OPEN, buyer=_ledger(budget=200000), directory={"ovr": 108, "banned": 0})
        ev = evaluate_request(self._req(), ctx)
        assert ev.ok and ev.price_k == 160000 and not ev.warnings

    def test_no_history_warns(self):
        ev = evaluate_request(self._req(), RequestContext(settings=OPEN, buyer=_ledger(budget=0)))
        assert ev.ok and _codes(ev.warnings) == {"SURCHARGE_NO_HISTORY", "BUDGET_EXCEEDED"}

    def test_blocks(self):
        dir_row = {"ovr": 111, "banned": 0}
        assert "SURCHARGE_NOT_HIGHER" in _codes(
            evaluate_request(self._req(), RequestContext(settings=OPEN, directory=dir_row)).blocks)
        assert _codes(evaluate_request(self._req(105), RequestContext(settings=OPEN)).blocks) \
            == {"SURCHARGE_NO_PRICE"}
        assert _codes(evaluate_request(self._req(99), RequestContext(settings=OPEN)).blocks) \
            == {"SURCHARGE_MIN_OVR"}


class TestUrn:
    S = WindowSettings(status="draft", urn_restricted_clubs=("Реал Мадрид",))

    def _sale(self, **kw):
        base = dict(kind="urn_sale", player_name="Some Player", from_club="Ливерпуль",
                    tm_price_k=25000, special_price_k=5350, sellable=True)
        base.update(kw)
        return TransferRequest(**base)

    def test_sale_allowed_in_draft(self):
        ctx = RequestContext(settings=self.S, seller=_ledger(), seller_core_after=7,
                             player_in_seller_squad=True)
        ev = evaluate_request(self._sale(), ctx)
        assert ev.ok and ev.price_k == 15100

    def test_unsellable_divisor(self):
        ev = evaluate_request(self._sale(sellable=False), RequestContext(settings=self.S))
        assert ev.price_k == 10100     # 30.35 / 3 = 10.116 → 10.1

    def test_sale_blocks(self):
        ctx = RequestContext(settings=self.S, seller=_ledger("Реал Мадрид", urn_sales=1),
                             seller_core_after=4)
        ev = evaluate_request(self._sale(from_club="Реал Мадрид"), ctx)
        assert _codes(ev.blocks) == {"URN_RESTRICTED", "URN_LIMIT", "CORE_RULE"}

    def test_sale_needs_prices(self):
        ev = evaluate_request(self._sale(tm_price_k=None), RequestContext(settings=self.S))
        assert _codes(ev.blocks) == {"INVALID_PRICE"}

    def test_buy_not_in_draft(self):
        req = TransferRequest(kind="urn_buy", player_name="Some Player", to_club="Челси")
        assert _codes(evaluate_request(req, RequestContext(settings=self.S)).blocks) == {"WINDOW_DRAFT"}

    def test_buy(self):
        item = {"kind": "urn_sale", "status": "approved", "from_club": "Ливерпуль",
                "tm_price_k": 25000, "special_price_k": 5350}
        s = WindowSettings(status="open")
        req = TransferRequest(kind="urn_buy", player_name="Some Player", to_club="Ливерпуль")
        ev = evaluate_request(req, RequestContext(settings=s, buyer=_ledger(budget=100000), urn_item=item))
        assert ev.ok and ev.price_k == 30350 and _codes(ev.warnings) == {"URN_OWN_BUYBACK"}

        ev = evaluate_request(req, RequestContext(settings=s, urn_item=item, urn_item_taken=True))
        assert _codes(ev.blocks) == {"URN_ITEM_TAKEN"}
        ev = evaluate_request(req, RequestContext(settings=s, urn_item=dict(item, status="pending_manager")))
        assert _codes(ev.blocks) == {"URN_ITEM_MISSING"}
