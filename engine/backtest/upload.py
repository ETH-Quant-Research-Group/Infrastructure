"""Reading a user's own history file (CSV) into bars or funding rates.

Formats are recognised, not configured:

- **Separator:** ``,`` ``;`` tab or ``|``.  With ``;``, a decimal comma
  (``1,5``) is read as ``1.5``.
- **Columns:** by name, whatever the case or punctuation (``open_time``,
  ``Timestamp``, ``Close``, ``Volume``, ``fundingRate`` …).  Without a
  header, bars are read as ``time, open, high, low, close[, volume]`` (the
  Bybit / Binance kline order) and funding as ``time, rate``.
- **Time:** Unix seconds, milliseconds, microseconds or nanoseconds (told
  apart by size), or a date / ISO text (``2025-01-01 08:00:00``,
  ``…T08:00:00Z``); without a zone it's UTC.
- **Timeframe** (bars): the most common spacing; it must be one the
  backtester supports (1m … 1d).

Then cleaned: sorted, duplicate times dropped (the last row wins), bad
candles dropped (a price ≤ 0, high below open / close / low, low above
open / close), and everything found reported — gaps, misaligned times,
funding that looks like percent — for the user to see before saving.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from data.types import FundingRate, TimeBar
from engine.backtest.history_db import INTERVAL_SECONDS

MAX_ROWS = 2_000_000
_MS = timedelta(milliseconds=1)
_EXAMPLES = 5

_ALIASES = {
    "time": (
        "timestamp",
        "time",
        "date",
        "datetime",
        "opentime",
        "starttime",
        "start",
        "ts",
        "t",
        "unix",
        "fundingtime",
        "fundingratetimestamp",
        "fundingtimestamp",
        "settlementtime",
        "calctime",
    ),
    "open": ("open", "o", "openprice"),
    "high": ("high", "h", "highprice"),
    "low": ("low", "l", "lowprice"),
    "close": ("close", "c", "closeprice"),
    "volume": ("volume", "vol", "v", "basevolume", "volumebase", "qty"),
    "trades": ("trades", "tradecount", "numberoftrades", "count", "ntrades"),
    "rate": ("fundingrate", "rate", "funding", "fr", "lastfundingrate"),
}
_POSITIONAL = {
    "bars": ("time", "open", "high", "low", "close", "volume"),
    "funding": ("time", "rate"),
}
_NEEDED = {
    "bars": ("time", "open", "high", "low", "close"),
    "funding": ("time", "rate"),
}
_DATE_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


class UploadError(ValueError):
    """The file can't be read as bars / funding at all."""


@dataclass
class ParsedUpload:
    kind: str  # bars | funding
    symbol: str
    interval: str | None
    rows: list[Any]  # TimeBar | FundingRate, oldest first
    detected: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """For the upload preview: what was understood and found."""
        first, last = (self.rows[0], self.rows[-1]) if self.rows else (None, None)
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "interval": self.interval,
            "rows": len(self.rows),
            "detected": self.detected,
            "issues": self.issues,
            "first": first.timestamp.isoformat() if first else None,
            "last": last.timestamp.isoformat() if last else None,
            "sample": [_row_dict(r) for r in self.rows[:3] + self.rows[-2:]],
        }


def _row_dict(r: Any) -> dict[str, str]:
    if isinstance(r, TimeBar):
        return {
            "time": r.timestamp.isoformat(),
            "open": str(r.open),
            "high": str(r.high),
            "low": str(r.low),
            "close": str(r.close),
            "volume": str(r.volume),
        }
    return {"time": r.timestamp.isoformat(), "rate": str(r.funding_rate)}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _number(cell: str, decimal_comma: bool) -> Decimal:
    text = cell.strip().replace(" ", "")
    if decimal_comma and "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {cell!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a number: {cell!r}")
    return value


def parse_time(cell: str) -> tuple[datetime, str]:
    """A time cell → (UTC datetime, the format it was in)."""
    text = cell.strip()
    try:
        value = float(text)
    except ValueError:
        pass
    else:
        for limit, unit, scale in (
            (1e17, "unix ns", 1e9),
            (1e14, "unix us", 1e6),
            (1e11, "unix ms", 1e3),
            (0, "unix s", 1.0),
        ):
            if abs(value) >= limit:
                return datetime.fromtimestamp(value / scale, tz=UTC), unit
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        fmt = "ISO"
    except ValueError:
        for f in _DATE_FORMATS:
            try:
                when, fmt = datetime.strptime(text, f), f
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"not a time: {cell!r}") from None
    return (
        when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    ), fmt


def _separator(text: str) -> str:
    """The separator that appears the same number of times on each of the
    first lines (most often wins) — ``,`` if none does.  Steadier than
    csv.Sniffer on ``;`` files with decimal commas."""
    lines = [ln for ln in text.splitlines()[:20] if ln.strip()]
    best, best_count = ",", 0
    for sep in (",", ";", "\t", "|"):
        counts = {ln.count(sep) for ln in lines}
        if len(counts) == 1 and (n := counts.pop()) > best_count:
            best, best_count = sep, n
    return best


def _read_table(
    text: str, kind: str
) -> tuple[list[list[str]], dict[str, int], dict[str, Any]]:
    delimiter = _separator(text)
    table = [
        row
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if not table:
        raise UploadError("the file is empty")

    head = table[0]
    numeric = 0
    for cell in head:
        try:
            float(cell)
            numeric += 1
        except ValueError:
            pass
    header = numeric < len(head) / 2
    columns: dict[str, int] = {}
    if header:
        names = [_norm(c) for c in head]
        for field_name, aliases in _ALIASES.items():
            for alias in aliases:
                if alias in names:
                    columns[field_name] = names.index(alias)
                    break
        table = table[1:]
    else:
        columns = {name: i for i, name in enumerate(_POSITIONAL[kind]) if i < len(head)}
    missing = [f for f in _NEEDED[kind] if f not in columns]
    if missing:
        found = ", ".join(head) if header else f"{len(head)} unnamed columns"
        raise UploadError(
            f"no {' / '.join(missing)} column found (the file has: {found})"
        )
    if len(table) > MAX_ROWS:
        raise UploadError(f"{len(table):,} rows - at most {MAX_ROWS:,} per file")
    detected = {
        "separator": {"\t": "tab"}.get(delimiter, delimiter),
        "header": header,
        "columns": {
            f: (head[i] if header else f"column {i + 1}") for f, i in columns.items()
        },
    }
    return table, columns, detected


def _dedupe(items: list[tuple[datetime, Any]], issues: list[str]) -> list[Any]:
    by_time: dict[datetime, Any] = {}
    for when, item in items:
        by_time[when] = item
    if len(by_time) < len(items):
        issues.append(
            f"{len(items) - len(by_time)} duplicate times dropped (the last row kept)"
        )
    return [by_time[t] for t in sorted(by_time)]


def parse_bars(text: str, symbol: str) -> ParsedUpload:
    """A bars file → cleaned :class:`TimeBar` rows at the detected timeframe."""
    table, col, detected = _read_table(text, "bars")
    decimal_comma = detected["separator"] == ";"
    issues: list[str] = []
    bad: list[str] = []
    raw: list[tuple[datetime, dict[str, Any]]] = []
    formats: Counter[str] = Counter()
    first_line = 2 if detected["header"] else 1
    for n, row in enumerate(table, first_line):
        try:
            when, fmt = parse_time(row[col["time"]])
            values: dict[str, Any] = {
                f: _number(row[col[f]], decimal_comma)
                for f in ("open", "high", "low", "close")
            }
            values["volume"] = (
                _number(row[col["volume"]], decimal_comma)
                if "volume" in col and row[col["volume"]].strip()
                else Decimal(0)
            )
            values["trades"] = (
                int(_number(row[col["trades"]], decimal_comma))
                if "trades" in col and row[col["trades"]].strip()
                else 0
            )
        except (ValueError, IndexError) as exc:
            bad.append(
                f"line {n}: "
                + (str(exc) if not isinstance(exc, IndexError) else "too few columns")
            )
            continue
        o, h, lo, c = values["open"], values["high"], values["low"], values["close"]
        if min(o, h, lo, c) <= 0 or h < max(o, c, lo) or lo > min(o, c):
            bad.append(f"line {n}: not a valid candle (o {o} h {h} l {lo} c {c})")
            continue
        formats[fmt] += 1
        raw.append((when, values))
    if bad:
        issues.append(
            f"{len(bad)} rows dropped: "
            + "; ".join(bad[:_EXAMPLES])
            + (" …" if len(bad) > _EXAMPLES else "")
        )
    rows = _dedupe(raw, issues)
    times = sorted({w for w, _ in raw})
    if len(times) < 2:
        raise UploadError("fewer than 2 usable rows - can't tell the timeframe")
    spacing = Counter(int((b - a).total_seconds()) for a, b in pairwise(times))
    step = spacing.most_common(1)[0][0]
    interval = next((iv for iv, s in INTERVAL_SECONDS.items() if s == step), None)
    if interval is None:
        raise UploadError(
            f"rows are {step} s apart - supported timeframes:"
            f" {', '.join(INTERVAL_SECONDS)}"
        )
    detected["time_format"] = formats.most_common(1)[0][0]
    detected["timeframe"] = interval

    gaps = sum(
        int((b - a).total_seconds()) // step - 1
        for a, b in pairwise(times)
        if (b - a).total_seconds() > step
    )
    if gaps:
        issues.append(f"{gaps} missing {interval} bars (gaps in the data)")
    off = sum(1 for t in times if int(t.timestamp()) % step)
    if off:
        issues.append(
            f"{off} bars don't start on a whole {interval} (UTC) - longer timeframes"
            " built from them may skip buckets"
        )
    bars = [
        TimeBar(
            symbol=symbol,
            open=v["open"],
            high=v["high"],
            low=v["low"],
            close=v["close"],
            volume=v["volume"],
            trade_count=v["trades"],
            timestamp=t,
            close_time=t + timedelta(seconds=step) - _MS,
            interval_seconds=step,
        )
        for t, v in zip(times, rows, strict=True)
    ]
    return ParsedUpload("bars", symbol, interval, bars, detected, issues)


def parse_funding(text: str, symbol: str) -> ParsedUpload:
    """A funding file → cleaned :class:`FundingRate` rows (per-period rates)."""
    table, col, detected = _read_table(text, "funding")
    decimal_comma = detected["separator"] == ";"
    issues: list[str] = []
    bad: list[str] = []
    raw: list[tuple[datetime, Decimal]] = []
    formats: Counter[str] = Counter()
    first_line = 2 if detected["header"] else 1
    for n, row in enumerate(table, first_line):
        try:
            when, fmt = parse_time(row[col["time"]])
            rate = _number(row[col["rate"]].strip().removesuffix("%"), decimal_comma)
        except (ValueError, IndexError) as exc:
            bad.append(
                f"line {n}: "
                + (str(exc) if not isinstance(exc, IndexError) else "too few columns")
            )
            continue
        formats[fmt] += 1
        raw.append((when, rate))
    if bad:
        issues.append(
            f"{len(bad)} rows dropped: "
            + "; ".join(bad[:_EXAMPLES])
            + (" …" if len(bad) > _EXAMPLES else "")
        )
    rates = _dedupe(raw, issues)
    if not rates:
        raise UploadError("no usable rows")
    times = sorted({w for w, _ in raw})
    detected["time_format"] = formats.most_common(1)[0][0]
    typical = sorted(abs(r) for r in rates)[len(rates) // 2]
    if typical > Decimal("0.01"):
        issues.append(
            f"the typical rate is {typical} - that looks like percent (Bybit gives"
            " 0.0001 for 0.01 %); tick 'rates are in percent' if so"
        )
    funding = [
        FundingRate(symbol=symbol, funding_rate=r, mark_price=Decimal(0), timestamp=t)
        for t, r in zip(times, rates, strict=True)
    ]
    return ParsedUpload("funding", symbol, None, funding, detected, issues)


def to_fraction(parsed: ParsedUpload) -> ParsedUpload:
    """Funding given in percent → per-period fractions (0.01 % → 0.0001)."""
    parsed.rows = [
        FundingRate(
            symbol=r.symbol,
            funding_rate=r.funding_rate / 100,
            mark_price=r.mark_price,
            timestamp=r.timestamp,
        )
        for r in parsed.rows
    ]
    return parsed
