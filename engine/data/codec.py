from __future__ import annotations

import dataclasses
import json
import types
import typing
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from data.types import DollarBar, FundingRate, TickBar, TimeBar, Trade, VolumeBar
from interfaces.signals import TargetPosition

if TYPE_CHECKING:
    from engine.types import BusEvent

"""
This is just basic Encoding/Decoding for NATS server for proper communication.

"""


_TYPE_MAP: dict[str, type] = {
    "TimeBar": TimeBar,
    "TickBar": TickBar,
    "VolumeBar": VolumeBar,
    "DollarBar": DollarBar,
    "Trade": Trade,
    "FundingRate": FundingRate,
}

# Extra namespace for get_type_hints() — resolves TYPE_CHECKING-guarded imports.
_HINTS_NS: dict[str, type] = {"datetime": datetime, "Decimal": Decimal}


def _serialize(val: object) -> object:
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    return val


def encode(event: BusEvent) -> bytes:
    """Serialize a BusEvent to JSON bytes with a ``"type"`` discriminator."""
    d: dict[str, object] = {"type": type(event).__name__}
    for field in dataclasses.fields(event):
        d[field.name] = _serialize(getattr(event, field.name))
    return json.dumps(d).encode()


def _coerce(val: object, hint: type) -> object:
    """Cast a JSON-decoded value to the target type indicated by *hint*."""
    args: tuple[type, ...] = ()
    origin = typing.get_origin(hint)
    if origin is typing.Union or isinstance(hint, types.UnionType):
        args = typing.get_args(hint)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if val is None:
            return None
        if non_none:
            return _coerce(val, non_none[0])
    if hint is Decimal:
        return Decimal(str(val))
    if hint is datetime:
        return datetime.fromisoformat(str(val))
    return val


def decode(data: bytes) -> BusEvent:
    """Deserialize JSON bytes back to the appropriate BusEvent dataclass."""
    d: dict[str, object] = json.loads(data)
    type_name = str(d["type"])
    cls = _TYPE_MAP[type_name]
    hints = typing.get_type_hints(cls, localns=_HINTS_NS)
    kwargs: dict[str, object] = {
        field.name: _coerce(d[field.name], hints[field.name])
        for field in dataclasses.fields(cls)
        if field.name in d
    }
    return cast("BusEvent", cls(**kwargs))


def encode_target(target: TargetPosition) -> bytes:
    """Serialize a TargetPosition to JSON bytes."""
    return json.dumps(
        {
            "symbol": target.symbol,
            "quantity": str(target.quantity),
            "strategy_id": target.strategy_id,
        }
    ).encode()


def decode_target(data: bytes) -> TargetPosition:
    """Deserialize JSON bytes back to a TargetPosition."""
    d: dict[str, str] = json.loads(data)
    return TargetPosition(
        symbol=d["symbol"],
        quantity=Decimal(d["quantity"]),
        strategy_id=d["strategy_id"],
    )
