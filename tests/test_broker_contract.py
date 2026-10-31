"""Every broker must implement the full BaseBroker contract."""

from __future__ import annotations

import pytest

from execution.brokers.bybit import BybitBroker
from execution.brokers.bybit_spot import BybitSpotBroker
from interfaces.broker import BaseBroker

REQUIRED = {
    "place_order", "cancel_order", "cancel_all_orders", "order_status", "order_history",
    "open_orders", "position", "list_positions", "instrument_rules", "funding_settlements",
    "aclose",
}


def test_the_contract_is_what_the_workers_rely_on() -> None:
    assert BaseBroker.__abstractmethods__ == REQUIRED


@pytest.mark.parametrize("cls", [BybitBroker, BybitSpotBroker])
def test_bybit_brokers_meet_the_contract(cls: type[BaseBroker]) -> None:
    assert not cls.__abstractmethods__
    cls(api_key="k", api_secret="s", demo=True)  # type: ignore[call-arg]


def test_obsolete_paper_broker_cannot_be_created() -> None:
    from execution.brokers.paper import PaperBroker

    with pytest.raises(TypeError, match="order_status"):
        PaperBroker()  # type: ignore[abstract]


def test_a_broker_missing_a_method_cannot_be_created() -> None:
    class Partial(BaseBroker):
        async def place_order(self, order):  # type: ignore[no-untyped-def]
            ...

    with pytest.raises(TypeError, match="instrument_rules"):
        Partial()  # type: ignore[abstract]


def test_spot_broker_pays_no_funding() -> None:
    import asyncio
    from datetime import UTC, datetime

    broker = BybitSpotBroker(api_key="k", api_secret="s", demo=True)
    now = datetime.now(UTC)
    assert asyncio.run(broker.funding_settlements(now, now)) == []
