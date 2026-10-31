"""Order-size rules per exchange and symbol — kept in memory, never fetched
per order.

- :class:`InstrumentCache` (consolidator): every symbol's rules, loaded from
  Bybit at start-up and refreshed every few hours.  Each order is rounded
  and checked against it with a dict lookup — no API call per order.
- :class:`OrderSizing` (strategy containers): the rules for the strategy's
  symbols, asked once from the consolidator at start-up (NATS
  ``control.instruments.rules``) and refreshed now and then.  Strategies
  read it synchronously: ``self.order_sizing.round_qty(...)``.

An order is **dropped** (never sent, reported as ``DROPPED`` with the
reason) when its symbol has no known rules, isn't trading, is below the
minimum quantity or order value, or is above the largest market order.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from execution.types import InstrumentRules

if TYPE_CHECKING:
    from collections.abc import Iterable

    from execution.types import Order

log = logging.getLogger(__name__)


def round_down(qty: Decimal, step: Decimal) -> Decimal:
    """Round the size of *qty* down to a multiple of *step*, keeping its sign."""
    rounded = (abs(qty) // step) * step
    return rounded if qty >= 0 else -rounded


def check_order(
    rules: InstrumentRules | None, symbol: str, qty: Decimal, price: Decimal | None = None
) -> str | None:
    """Why an order of (unsigned) *qty* at about *price* can't be sent, or
    ``None`` if it can.  The value check is skipped without a price."""
    if rules is None:
        return f"no order-size rules known for {symbol}"
    if not rules.trading:
        return f"{symbol} is not trading right now"
    if qty <= 0:
        return f"quantity {qty} is not positive"
    if rules.min_qty is not None and qty < rules.min_qty:
        return f"quantity {qty} below the minimum order size {rules.min_qty} for {symbol}"
    if rules.max_market_qty is not None and qty > rules.max_market_qty:
        return f"quantity {qty} above the largest market order {rules.max_market_qty} for {symbol}"
    if price and rules.min_notional is not None and qty * price < rules.min_notional:
        return (
            f"order value {qty * price} below the minimum {rules.min_notional} for {symbol}"
        )
    return None


def rules_to_dict(exchange: str, rules: InstrumentRules) -> dict[str, Any]:
    def s(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "exchange": exchange,
        "symbol": rules.symbol,
        "qty_step": str(rules.qty_step),
        "min_qty": s(rules.min_qty),
        "max_market_qty": s(rules.max_market_qty),
        "min_notional": s(rules.min_notional),
        "trading": rules.trading,
    }


def rules_from_dict(d: dict[str, Any]) -> tuple[str, InstrumentRules]:
    def dec(key: str) -> Decimal | None:
        return None if d.get(key) is None else Decimal(d[key])

    return d["exchange"], InstrumentRules(
        symbol=d["symbol"],
        qty_step=Decimal(d["qty_step"]),
        min_qty=dec("min_qty"),
        max_market_qty=dec("max_market_qty"),
        min_notional=dec("min_notional"),
        trading=bool(d.get("trading", True)),
    )


class InstrumentCache:
    """Every symbol's order-size rules, per exchange, held in memory.

    *fallback_steps* (exchange → symbol → step) is used only for symbols
    whose rules couldn't be loaded — a step and nothing else, so only the
    rounding applies.
    """

    def __init__(self, fallback_steps: dict[str, dict[str, Decimal]] | None = None) -> None:
        self._rules: dict[tuple[str, str], InstrumentRules] = {}
        self._fallback = {
            (exchange, symbol): InstrumentRules(symbol=symbol, qty_step=step)
            for exchange, steps in (fallback_steps or {}).items()
            for symbol, step in steps.items()
        }
        self.loaded_at: dict[str, datetime] = {}

    async def load(self, brokers: dict[str, Any]) -> dict[str, int]:
        """(Re)load the rules from every broker that has ``instrument_rules``.
        A broker that fails keeps its previous rules (logged).  Returns how
        many symbols were loaded per exchange."""
        counts: dict[str, int] = {}
        for exchange, broker in brokers.items():
            if not hasattr(broker, "instrument_rules"):
                continue
            try:
                rules = await broker.instrument_rules()
            except Exception as exc:
                log.warning("could not load order-size rules for %s: %s", exchange, exc)
                continue
            for key in [k for k in self._rules if k[0] == exchange]:
                del self._rules[key]
            for r in rules:
                self._rules[(exchange, r.symbol)] = r
            self.loaded_at[exchange] = datetime.now(UTC)
            counts[exchange] = len(rules)
        return counts

    @property
    def loaded(self) -> bool:
        return bool(self.loaded_at)

    def rules(self, exchange: str, symbol: str) -> InstrumentRules | None:
        return self._rules.get((exchange, symbol)) or self._fallback.get((exchange, symbol))

    def quantize(self, exchange: str, symbol: str, qty: Decimal) -> Decimal:
        """*qty* rounded down to the symbol's step (unchanged if unknown —
        :meth:`check` then drops the order)."""
        rules = self.rules(exchange, symbol)
        return qty if rules is None else round_down(qty, rules.qty_step)

    def max_market_qty(self, exchange: str, symbol: str) -> Decimal | None:
        """The largest market order for *symbol*, if known — the
        consolidator's ``max_order_qty`` hook (guard closes are split)."""
        rules = self.rules(exchange, symbol)
        return None if rules is None else rules.max_market_qty

    def check(self, exchange: str, order: Order, price: Decimal | None) -> str | None:
        """Why *order* can't be sent on *exchange*, or ``None`` — the
        consolidator's ``order_check`` hook."""
        return check_order(self.rules(exchange, order.symbol), order.symbol, order.quantity, price)

    def as_dicts(self, symbols: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """The rules (all, or only for *symbols*) as plain dicts, for NATS."""
        wanted = set(symbols) if symbols is not None else None
        keys = set(self._rules) | set(self._fallback)
        return [
            rules_to_dict(exchange, self.rules(exchange, symbol))  # type: ignore[arg-type]
            for exchange, symbol in sorted(keys)
            if wanted is None or symbol in wanted
        ]


class OrderSizing:
    """Order-size rules a strategy can read — all local, no network call.

    Filled by the strategy worker from the consolidator; available to the
    strategy as ``self.order_sizing``.  Unknown symbols give ``None`` /
    a reason from :meth:`check`, like the consolidator would.
    """

    def __init__(self, rules: dict[tuple[str, str], InstrumentRules] | None = None) -> None:
        self._rules = dict(rules or {})

    @classmethod
    def from_dicts(cls, items: Iterable[dict[str, Any]]) -> OrderSizing:
        sizing = cls()
        sizing.update(items)
        return sizing

    def update(self, items: Iterable[dict[str, Any]]) -> None:
        """Replace the rules in place (the strategy's reference stays valid)."""
        self._rules = {
            (exchange, rules.symbol): rules
            for exchange, rules in (rules_from_dict(d) for d in items)
        }

    def rules(self, exchange: str, symbol: str) -> InstrumentRules | None:
        """The rules for *symbol* on *exchange* (e.g. ``"bybit_spot"``)."""
        return self._rules.get((exchange, symbol))

    def round_qty(self, exchange: str, symbol: str, qty: Decimal) -> Decimal:
        """*qty* (signed) rounded down to a valid size; ``0`` if that's below
        the minimum or the symbol is unknown — i.e. what would actually be
        sent (``0`` = nothing)."""
        rules = self.rules(exchange, symbol)
        if rules is None:
            return Decimal(0)
        rounded = round_down(qty, rules.qty_step)
        if rules.min_qty is not None and abs(rounded) < rules.min_qty:
            return Decimal(0)
        return rounded

    def check(
        self, exchange: str, symbol: str, qty: Decimal, price: Decimal | None = None
    ) -> str | None:
        """Why an order of signed *qty* would be dropped, or ``None`` if it
        would be sent.  Checks the size after rounding, like the consolidator."""
        rules = self.rules(exchange, symbol)
        size = abs(qty) if rules is None else abs(round_down(qty, rules.qty_step))
        return check_order(rules, symbol, size, price)

    def __len__(self) -> int:
        return len(self._rules)
