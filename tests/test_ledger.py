from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import TYPE_CHECKING

from engine.order.consolidator import OrderConsolidator
from engine.order.ledger import Holding, LedgerDB, apply_fill, fees_in_quote
from execution.types import (
    FillConfirmation,
    FillStatus,
    OrderSide,
)
from tests.fakes import FakeBroker
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    from pathlib import Path

# Bybit v5 orderLinkId: max 36 chars, letters / digits / "-" / "_".
_ORDER_LINK_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


def test_new_strategies_get_sequential_ids(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    assert ledger.strategy_num("alpha") == 1
    assert ledger.strategy_num("beta") == 2
    assert ledger.strategy_num("alpha") == 1


def test_counters_increment_per_strategy(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    assert ledger.next_order_link_id("alpha") == "1_1"
    assert ledger.next_order_link_id("alpha") == "1_2"
    assert ledger.next_order_link_id("beta") == "2_1"
    assert ledger.next_order_link_id("alpha") == "1_3"


def test_ids_and_counters_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = LedgerDB(path)
    ledger.next_order_link_id("alpha")
    ledger.next_order_link_id("alpha")
    ledger.next_order_link_id("beta")
    ledger.close()

    reopened = LedgerDB(path)
    assert reopened.next_order_link_id("alpha") == "1_3"
    assert reopened.next_order_link_id("beta") == "2_2"
    assert reopened.next_order_link_id("gamma") == "3_1"


def test_tags_fit_bybit_rules_for_any_name(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = ledger.next_order_link_id("Funding Arb / ETH ✓ " + "x" * 100)
    assert _ORDER_LINK_ID.match(tag)


_FakeBroker = FakeBroker


def test_consolidator_tags_each_order_with_strategy_counter(tmp_path: Path) -> None:
    broker = _FakeBroker()
    consolidator = OrderConsolidator(
        target_queue=asyncio.Queue(),
        brokers={"fake": broker},
        default_exchange="fake",
        ledger=LedgerDB(tmp_path / "ledger.db"),
    )

    async def _run() -> None:
        for sid in ("alpha", "beta", "alpha"):
            await consolidator._reconcile(
                TargetPosition(symbol="ETHUSDT", quantity=Decimal(1), strategy_id=sid)
            )

    asyncio.run(_run())
    assert [o.client_order_id for o in broker.placed] == ["1_1", "2_1", "1_2"]


# ------------------------------------------------------------- average cost


def _replay(*fills: tuple[str, str]) -> tuple[Decimal, Decimal, Decimal]:
    qty = avg = realized = Decimal(0)
    for delta, price in fills:
        qty, avg, realized = apply_fill(qty, avg, realized, Decimal(delta), Decimal(price))
    return qty, avg, realized


def _d(qty: int, avg: int, realized: int) -> tuple[Decimal, Decimal, Decimal]:
    return Decimal(qty), Decimal(avg), Decimal(realized)


def test_adding_updates_average_entry_only() -> None:
    assert _replay(("1", "2000"), ("1", "2200")) == _d(2, 2100, 0)


def test_reducing_realizes_and_keeps_average_entry() -> None:
    assert _replay(("1", "2000"), ("1", "2200"), ("-1", "2300")) == _d(1, 2100, 200)
    assert _replay(
        ("1", "2000"), ("1", "2200"), ("-1", "2300"), ("-1", "2000")
    ) == _d(0, 0, 100)


def test_short_profits_when_price_falls() -> None:
    assert _replay(("-2", "100"), ("1", "90")) == _d(-1, 100, 10)


def test_flip_closes_old_side_and_opens_at_fill_price() -> None:
    assert _replay(("1", "100"), ("-3", "110")) == _d(-2, 110, 10)


def test_fees_in_quote_converts_base_coin_fees() -> None:
    fees = {"USDT": Decimal("0.5"), "ETH": Decimal("0.001")}
    assert fees_in_quote("ETHUSDT", fees, Decimal("2000")) == Decimal("2.5")


# ------------------------------------------------------------ trades/holdings


def _fill(
    tag: str,
    qty: str,
    price: str = "2000",
    status: FillStatus = FillStatus.FILLED,
    fees: dict[str, Decimal] | None = None,
    exchange: str = "bybit_demo",
) -> FillConfirmation:
    return FillConfirmation(
        strategy_id="alpha",
        symbol="ETHUSDT",
        quantity=Decimal(qty),
        fill_price=Decimal(price) if qty != "0" else Decimal(0),
        exchange=exchange,
        status=status,
        fees=fees if fees is not None else {"USDT": Decimal("0.1")},
        order_id=f"bybit-{tag}",
        order_link_id=tag,
    )


def _pending(ledger: LedgerDB, side: OrderSide = OrderSide.BUY, qty: str = "1") -> str:
    tag = ledger.next_order_link_id("alpha")
    ledger.record_pending(tag, "alpha", "bybit_demo", "ETHUSDT", side, Decimal(qty))
    return tag


def test_pending_trade_is_logged_then_filled(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = _pending(ledger)

    (trade,) = ledger.trades("alpha")
    assert (trade.order_link_id, trade.status, trade.order_id) == (tag, "pending", None)

    ledger.record_accepted(tag, "bybit-1")
    assert ledger.trades("alpha")[0].order_id == "bybit-1"

    assert ledger.record_result(_fill(tag, "1"))
    (trade,) = ledger.trades("alpha")
    assert trade.status == "filled"
    assert trade.filled_qty == Decimal("1")
    assert trade.avg_price == Decimal("2000")
    assert trade.fees == {"USDT": Decimal("0.1")}
    assert ledger.holdings("alpha") == [
        Holding(
            exchange="bybit_demo",
            symbol="ETHUSDT",
            quantity=Decimal("1"),
            avg_entry_price=Decimal("2000"),
            realized_pnl=Decimal("0"),
            fees_paid=Decimal("0.1"),
        )
    ]


def test_round_trip_realizes_pnl_and_sums_fees(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    ledger.record_result(_fill(_pending(ledger), "1", "2000"))
    ledger.record_result(_fill(_pending(ledger, OrderSide.SELL), "-1", "2100"))

    (holding,) = ledger.holdings("alpha")
    assert holding.quantity == 0
    assert holding.realized_pnl == Decimal("100")
    assert holding.fees_paid == Decimal("0.2")
    assert [t.status for t in ledger.trades("alpha")] == ["filled", "filled"]


def test_spot_base_coin_fee_counts_as_fee_not_as_cost(tmp_path: Path) -> None:
    """Buy 1 ETH @ 100, 0.001 ETH fee → hold 0.999; sell it @ 110, 0.1 USDT fee.
    Cash: paid 100, got 109.89 − 0.1 → net 9.79."""
    ledger = LedgerDB(tmp_path / "ledger.db")
    ledger.record_result(
        _fill(_pending(ledger), "0.999", "100", fees={"ETH": Decimal("0.001")},
              exchange="bybit_spot")
    )
    ledger.record_result(
        _fill(_pending(ledger, OrderSide.SELL), "-0.999", "110",
              fees={"USDT": Decimal("0.1")}, exchange="bybit_spot")
    )

    (holding,) = ledger.holdings("alpha")
    assert holding.realized_pnl == Decimal("9.990")
    assert holding.fees_paid == Decimal("0.200")
    assert holding.realized_pnl - holding.fees_paid == Decimal("9.79")


def test_rejected_trade_changes_no_holding(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = _pending(ledger)
    ledger.record_result(_fill(tag, "0", status=FillStatus.REJECTED, fees={}))

    assert ledger.trades("alpha")[0].status == "rejected"
    assert ledger.holdings("alpha") == []


def test_same_result_is_never_applied_twice(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = _pending(ledger)
    assert ledger.record_result(_fill(tag, "1"))
    assert not ledger.record_result(_fill(tag, "1"))

    assert ledger.holdings("alpha")[0].quantity == Decimal("1")


def test_unknown_trade_can_still_be_resolved(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    tag = _pending(ledger)
    ledger.record_result(_fill(tag, "0", status=FillStatus.UNKNOWN, fees={}))
    assert [t.order_link_id for t in ledger.unfinished_trades()] == [tag]

    assert ledger.record_result(_fill(tag, "1"))
    assert ledger.unfinished_trades() == []
    assert ledger.holdings("alpha")[0].quantity == Decimal("1")


def test_trade_log_and_holdings_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger = LedgerDB(path)
    ledger.record_result(_fill(_pending(ledger), "1"))
    pending_tag = _pending(ledger)
    ledger.close()

    reopened = LedgerDB(path)
    assert reopened.holdings("alpha")[0].quantity == Decimal("1")
    assert [t.order_link_id for t in reopened.unfinished_trades()] == [pending_tag]
    assert len(reopened.trades("alpha")) == 2


def test_unknown_strategy_reads_are_empty(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    assert ledger.holdings("nobody") == []
    assert ledger.trades("nobody") == []
