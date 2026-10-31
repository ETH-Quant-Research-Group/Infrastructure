from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from engine.order.funding import UNATTRIBUTED, FundingDB, collect_funding, split
from engine.order.ledger import LedgerDB
from engine.order.performance import PnLHistoryDB, strategy_pnl
from execution.brokers._bybit_common import parse_funding_settlement
from execution.brokers.bybit import BybitBroker
from execution.types import FillConfirmation, FillStatus, FundingSettlement, OrderSide
from workers import snapshot_all
from workers.strategy_info import StrategyInfoWorker

if TYPE_CHECKING:
    from pathlib import Path

_T = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)  # a settlement time


def _settlement(amount: str, position: str | None, sid: str = "s1",
                symbol: str = "ETHUSDT", at: datetime = _T) -> FundingSettlement:
    return FundingSettlement(
        settlement_id=sid, symbol=symbol, settled_at=at, amount=Decimal(amount),
        position=None if position is None else Decimal(position),
    )


def _d(**shares: str) -> dict[str, Decimal]:
    return {name.replace("unattributed", UNATTRIBUTED): Decimal(v) for name, v in shares.items()}


# --------------------------------------------------------------------- split


def test_one_strategy_holding_the_whole_position_gets_everything() -> None:
    assert split(_settlement("1.25", "-5"), {"arb": Decimal(-5)}) == _d(arb="1.25")


def test_shares_are_proportional_to_positions() -> None:
    shares = split(_settlement("3", "-6"), {"a": Decimal(-4), "b": Decimal(-2)})
    assert shares == _d(a="2", b="1")


def test_opposite_sides_still_add_up_to_the_payment() -> None:
    # account long 1 = a long 3 + b short 2; longs paid 0.5 per unit
    shares = split(_settlement("-0.5", "1"), {"a": Decimal(3), "b": Decimal(-2)})
    assert shares == _d(a="-1.5", b="1")
    assert sum(shares.values()) == Decimal("-0.5")


def test_position_no_strategy_owns_goes_to_unattributed() -> None:
    shares = split(_settlement("5", "-5"), {"arb": Decimal(-3)})
    assert shares == _d(arb="3", unattributed="2")


def test_rounding_breadcrumbs_go_to_unattributed() -> None:
    positions = {"a": Decimal(1), "b": Decimal(1), "c": Decimal(1)}
    shares = split(_settlement("1", "3"), positions)
    assert shares == _d(a="0.33333333", b="0.33333333", c="0.33333333",
                        unattributed="0.00000001")
    assert sum(shares.values()) == Decimal(1)


def test_unreported_account_position_uses_the_strategies_alone() -> None:
    assert split(_settlement("2", None), {"a": Decimal(1), "b": Decimal(3)}) == _d(a="0.5", b="1.5")


def test_payment_on_a_flat_account_is_all_unattributed() -> None:
    assert split(_settlement("0.3", "0"), {}) == _d(unattributed="0.3")
    assert split(_settlement("0", "0"), {}) == {}


# ----------------------------------------------------------------- Bybit data


def test_settlement_is_parsed_from_the_transaction_log() -> None:
    s = parse_funding_settlement({
        "id": "abc", "symbol": "ETHUSDT", "type": "SETTLEMENT", "side": "Sell",
        "size": "5", "change": "1.25", "transactionTime": str(int(_T.timestamp() * 1000)),
    })
    assert (s.settlement_id, s.symbol, s.settled_at, s.amount, s.position) == (
        "abc", "ETHUSDT", _T, Decimal("1.25"), Decimal(-5)
    )
    long = parse_funding_settlement({"id": "x", "side": "Buy", "size": "2", "change": "-1"})
    assert long.position == Decimal(2)
    assert parse_funding_settlement({"id": "y", "change": "1"}).position is None


def test_broker_asks_for_settlements_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = {"id": "s1", "symbol": "ETHUSDT", "side": "Sell", "size": "5",
                "change": "1.25", "transactionTime": "1760774400000"}
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [item]}})

    broker = BybitBroker(api_key="k", api_secret="s", demo=True)
    broker._client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    (s,) = asyncio.run(broker.funding_settlements(_T - timedelta(days=1), _T))

    params = seen[0].url.params
    assert seen[0].url.path == "/v5/account/transaction-log"
    assert (params["type"], params["category"], params["accountType"]) == (
        "SETTLEMENT", "linear", "UNIFIED"
    )
    assert s.amount == Decimal("1.25")


# --------------------------------------------------------- positions at time


def _fill(ledger: LedgerDB, strategy: str, qty: str, at: datetime,
          exchange: str = "bybit_demo", status: FillStatus = FillStatus.FILLED) -> None:
    side = OrderSide.BUY if Decimal(qty) > 0 else OrderSide.SELL
    tag = ledger.begin_order(strategy, exchange, "ETHUSDT", side, abs(Decimal(qty)))
    filled = Decimal(qty) if status is not FillStatus.REJECTED else Decimal(0)
    ledger.record_result(FillConfirmation(
        strategy_id=strategy, symbol="ETHUSDT", quantity=filled,
        fill_price=Decimal(100) if filled else Decimal(0), exchange=exchange,
        status=status, fees={"USDT": Decimal("0.1")} if filled else {}, order_link_id=tag,
    ), at=at)


def test_positions_at_count_only_perp_fills_up_to_that_time(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "arb", "-5", _T - timedelta(hours=1))
    _fill(ledger, "arb", "5", _T - timedelta(hours=1), exchange="bybit_spot")  # spot leg
    _fill(ledger, "arb", "-1", _T - timedelta(hours=1), status=FillStatus.REJECTED)
    _fill(ledger, "arb", "-2", _T + timedelta(seconds=1))  # after the settlement
    _fill(ledger, "mom", "1", _T - timedelta(minutes=5))
    _fill(ledger, "mom", "-1", _T - timedelta(minutes=1))  # flat again

    assert ledger.positions_at("bybit_demo", "ETHUSDT", _T) == {"arb": Decimal(-5)}


# ------------------------------------------------------------------ storage


def test_funding_db_records_each_settlement_once(tmp_path: Path) -> None:
    db = FundingDB(tmp_path / "funding.db")
    s = _settlement("1.25", "-5")
    assert db.record(s, _d(arb="1", unattributed="0.25"))
    assert not db.record(s, _d(arb="1", unattributed="0.25"))
    db.record(_settlement("0.5", "-5", sid="s2", symbol="LINKUSDT"), _d(arb="0.5"))

    assert db.by_symbol("arb") == {"ETHUSDT": Decimal(1), "LINKUSDT": Decimal("0.5")}
    assert db.total("arb") == Decimal("1.5")
    assert db.total(UNATTRIBUTED) == Decimal("0.25")
    assert db.last_settled_at() == _T
    db.close()
    assert FundingDB(tmp_path / "funding.db", readonly=True).total("arb") == Decimal("1.5")


def test_cached_totals_follow_new_settlements_from_another_process(tmp_path: Path) -> None:
    writer = FundingDB(tmp_path / "funding.db")
    writer.record(_settlement("1", "-5"), _d(arb="1"))
    reader = FundingDB(tmp_path / "funding.db", readonly=True)
    assert reader.total("arb") == Decimal(1)
    assert writer.total("arb") == Decimal(1)

    writer.record(_settlement("2", "-5", sid="s2"), _d(arb="1.5", unattributed="0.5"))
    assert reader.total("arb") == Decimal("2.5")  # picked up without a restart
    assert reader.total(UNATTRIBUTED) == Decimal("0.5")
    assert writer.by_symbol("arb") == {"ETHUSDT": Decimal("2.5")}
    assert reader.total("nobody") == Decimal(0)
    reader.close()
    writer.close()


class _FundingBroker:
    def __init__(self, settlements: list[FundingSettlement]) -> None:
        self.settlements = settlements
        self.asked: list[tuple[datetime, datetime]] = []

    async def funding_settlements(self, start: datetime, end: datetime) -> list[FundingSettlement]:
        self.asked.append((start, end))
        return self.settlements

    async def aclose(self) -> None:
        pass


def test_collect_splits_new_settlements_and_skips_known_ones(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "arb", "-4", _T - timedelta(hours=1))
    db = FundingDB(tmp_path / "funding.db")
    broker = _FundingBroker([_settlement("1.25", "-5")])
    now = _T + timedelta(hours=2)

    (recorded,) = asyncio.run(collect_funding(ledger, db, broker, now))
    assert recorded[1] == _d(arb="1", unattributed="0.25")
    assert asyncio.run(collect_funding(ledger, db, broker, now)) == []  # nothing new

    assert broker.asked[0][0] == now - timedelta(days=7)  # first run: a week back
    assert broker.asked[1][0] == _T - timedelta(hours=1)  # then: since the last one
    assert db.total("arb") == Decimal(1)


# ------------------------------------------------------------------ in PnL


def test_funding_is_part_of_net_pnl(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "arb", "-5", _T - timedelta(hours=1))
    db = FundingDB(tmp_path / "funding.db")
    db.record(_settlement("1.25", "-5"), _d(arb="1.25"))

    async def price(exchange: str, symbol: str) -> Decimal:
        return Decimal(100)  # unchanged: no unrealized

    point = asyncio.run(strategy_pnl(ledger, "arb", price, funding=db))
    assert point.funding == Decimal("1.25")
    assert point.net_pnl == Decimal("1.15")  # 0 realized + 0 unrealized + 1.25 − 0.1 fees


def _ledger_with_arb(tmp_path: Path) -> None:
    ledger = LedgerDB(tmp_path / "ledger.db")
    _fill(ledger, "arb", "-4", _T - timedelta(hours=1))
    ledger.close()


async def _price(exchange: str, symbol: str) -> Decimal:
    return Decimal(100)


def test_daily_run_collects_funding_before_recording_pnl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger_with_arb(tmp_path)
    brokers: dict[str, Any] = {"bybit_demo": _FundingBroker([_settlement("1.25", "-5")])}

    code = asyncio.run(snapshot_all.run(
        tmp_path / "ledger.db", tmp_path / "snapshots.db", tmp_path / "pnl_history.db",
        prices=_price, brokers=brokers,
    ))

    assert code == 0
    out = capsys.readouterr().out
    assert "Funding ETHUSDT 2026-10-18 08:00: 1.25 -> arb 1, _unattributed 0.25" in out
    assert "Funding _unattributed total so far: 0.25" in out
    assert "PnL arb: net 0.9 (realized 0, unrealized 0, funding 1, fees 0.1)" in out
    (point,) = PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True).history("arb")
    assert point.funding == Decimal("1")


def test_a_failing_job_does_not_stop_the_others(tmp_path: Path) -> None:
    _ledger_with_arb(tmp_path)

    class _Down(_FundingBroker):
        async def funding_settlements(self, start: datetime, end: datetime) -> list[FundingSettlement]:
            raise RuntimeError("Bybit unreachable")

    code = asyncio.run(snapshot_all.run(
        tmp_path / "ledger.db", tmp_path / "snapshots.db", tmp_path / "pnl_history.db",
        prices=_price, brokers={"bybit_demo": _Down([])},
    ))

    assert code == 2
    assert len(PnLHistoryDB(tmp_path / "pnl_history.db", readonly=True).history("arb")) == 1


def test_info_file_shows_funding_and_the_unattributed_breadcrumbs(tmp_path: Path) -> None:
    _ledger_with_arb(tmp_path)
    db = FundingDB(tmp_path / "funding.db")
    db.record(_settlement("1.25", "-5"), _d(arb="1", unattributed="0.25"))
    db.close()
    worker = StrategyInfoWorker(
        LedgerDB(tmp_path / "ledger.db", readonly=True), _price,
        tmp_path / "strategy_info.json", names=("arb",), funding=tmp_path / "funding.db",
    )

    asyncio.run(worker.write())

    data = json.loads((tmp_path / "strategy_info.json").read_text())
    (arb,) = data["strategies"]
    assert arb["totals"]["funding"] == "1"
    assert arb["totals"]["net_pnl"] == "0.9"  # 1 funding − 0.1 fees
    assert arb["funding_by_symbol"] == {"ETHUSDT": "1"}
    assert data["unattributed_funding"] == {"total": "0.25", "by_symbol": {"ETHUSDT": "0.25"}}
