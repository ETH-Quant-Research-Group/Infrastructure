"""The backtest history database (``history.db`` in the backtest cache).

Bars and funding rates per **dataset**: ``bybit`` (downloaded from Bybit —
the starter set and anything the Deploy page fetched) and the users' own
uploads, each under a name.  Amounts are stored as text, like the ledger,
so nothing is rounded.

- ``fetched`` records which months of which series were downloaded from
  Bybit and up to when (a month downloaded before it ended is only good up
  to then) — so a month that really has no data isn't fetched again.
- **Longer timeframes are built, not stored:** 1h bars give 2h … 1d exactly
  (:func:`aggregate`), so the 1h starter set serves every one of them.
- An upload brings its own order-size rules for symbols Bybit doesn't list.
- A dataset not saved ("for this backtest only") expires after a day.

The webapp writes; backtest containers open it **read-only** (the volume is
mounted read-only there): rollback journal, not WAL, since a WAL reader
must be able to write the ``-shm`` file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from data.types import FundingRate, TimeBar
from execution.types import InstrumentRules

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

BYBIT = "bybit"
DB_NAME = "history.db"
TEMPORARY_FOR = timedelta(days=1)
_MS = timedelta(milliseconds=1)

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,            -- 'bybit' | 'upload'
    created_at  TEXT NOT NULL,
    expires_at  TEXT,                     -- NULL = saved for good
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bars (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    ts          INTEGER NOT NULL,         -- bar open, ms UTC
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
    volume      TEXT NOT NULL,
    trades      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset_id, symbol, interval, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    ts          INTEGER NOT NULL,         -- settlement, ms UTC
    rate        TEXT NOT NULL,
    PRIMARY KEY (dataset_id, symbol, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetched (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- 'bars_<interval>' | 'funding'
    month       TEXT NOT NULL,            -- 'YYYY-MM'
    until       TEXT NOT NULL,
    complete    INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, symbol, kind, month)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rules (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    qty_step        TEXT NOT NULL,
    min_qty         TEXT,
    max_market_qty  TEXT,
    min_notional    TEXT,
    PRIMARY KEY (dataset_id, exchange, symbol)
) WITHOUT ROWID;
"""


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def month_key(dt: datetime) -> str:
    return f"{dt:%Y-%m}"


@dataclass(frozen=True, kw_only=True)
class DatasetInfo:
    name: str
    source: str
    created_at: datetime
    expires_at: datetime | None
    note: str
    # symbol → {"intervals": {interval: (first, last, count)},
    #           "funding": (first, last, count) | None}
    series: dict[str, dict[str, object]]

    @property
    def saved(self) -> bool:
        return self.expires_at is None


def aggregate(bars: list[TimeBar], interval: str) -> list[TimeBar]:
    """*bars* (one timeframe, oldest first) combined into *interval* bars,
    aligned to UTC midnight.  Only complete buckets are kept: a bucket with a
    missing sub-bar is left out rather than built from part of its data."""
    if not bars:
        return []
    size = INTERVAL_SECONDS[interval]
    base = bars[0].interval_seconds
    if size == base:
        return list(bars)
    if size % base:
        raise ValueError(f"{interval} can't be built from {base}s bars")
    per = size // base
    out: list[TimeBar] = []
    group: list[TimeBar] = []
    bucket = None
    for b in bars:
        start = int(b.timestamp.timestamp()) // size * size
        if start != bucket:
            if len(group) == per:
                out.append(_combine(group, bucket, size))  # type: ignore[arg-type]
            group, bucket = [], start
        group.append(b)
    if len(group) == per:
        out.append(_combine(group, bucket, size))  # type: ignore[arg-type]
    return out


def _combine(group: list[TimeBar], start: int, size: int) -> TimeBar:
    opened = datetime.fromtimestamp(start, tz=UTC)
    return TimeBar(
        symbol=group[0].symbol,
        open=group[0].open,
        high=max(b.high for b in group),
        low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum((b.volume for b in group), Decimal(0)),
        trade_count=sum(b.trade_count for b in group),
        timestamp=opened,
        close_time=opened + timedelta(seconds=size) - _MS,
        interval_seconds=size,
    )


def sources_for(interval: str, stored: Iterable[str]) -> list[str]:
    """Stored timeframes that can give *interval* bars, best first: the
    interval itself, then the longest one it's a multiple of."""
    size = INTERVAL_SECONDS[interval]
    usable = [
        s for s in stored if s in INTERVAL_SECONDS and size % INTERVAL_SECONDS[s] == 0
    ]
    return sorted(usable, key=lambda s: (s != interval, -INTERVAL_SECONDS[s]))


class HistoryDB:
    """``history.db``: open it writable (webapp) or ``readonly`` (backtests)."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        if readonly:
            try:
                self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlite3.OperationalError:
                # Read-only mount where even a shared lock can't be taken.
                self._conn = sqlite3.connect(
                    f"file:{path}?mode=ro&immutable=1", uri=True
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA foreign_keys=ON")
            self.dataset_id(BYBIT, source=BYBIT)

    @classmethod
    def open(cls, cache_dir: Path, *, readonly: bool = False) -> HistoryDB | None:
        """The database in *cache_dir*; ``None`` if read-only and missing."""
        path = cache_dir / DB_NAME
        if readonly and not path.exists():
            return None
        return cls(path, readonly=readonly)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ datasets

    def dataset_id(
        self, name: str, *, source: str = "upload", saved: bool = True, note: str = ""
    ) -> int:
        """The dataset's id, created if new (not in read-only mode)."""
        row = self._conn.execute(
            "SELECT id FROM datasets WHERE name = ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        if self.readonly:
            raise KeyError(f"no dataset {name!r}")
        now = datetime.now(UTC)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO datasets (name, source, created_at, expires_at, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    source,
                    now.isoformat(),
                    None if saved else (now + TEMPORARY_FOR).isoformat(),
                    note,
                ),
            )
        return int(cur.lastrowid or 0)

    def find(self, name: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM datasets WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else int(row[0])

    def save(self, name: str) -> None:
        """Keep a temporary dataset for good."""
        with self._conn:
            self._conn.execute(
                "UPDATE datasets SET expires_at = NULL WHERE name = ?", (name,)
            )

    def delete(self, name: str) -> bool:
        if name == BYBIT:
            raise ValueError("the bybit dataset can't be deleted")
        with self._conn:
            cur = self._conn.execute("DELETE FROM datasets WHERE name = ?", (name,))
        return cur.rowcount > 0

    def drop_expired(self) -> list[str]:
        now = datetime.now(UTC).isoformat()
        names = [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM datasets"
                " WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
        ]
        for name in names:
            self.delete(name)
        return names

    def datasets(self) -> list[DatasetInfo]:
        out = []
        for ds_id, name, source, created, expires, note in self._conn.execute(
            "SELECT id, name, source, created_at, expires_at, note"
            " FROM datasets ORDER BY id"
        ).fetchall():
            series: dict[str, dict[str, object]] = {}
            for symbol, interval, first, last, count in self._conn.execute(
                "SELECT symbol, interval, min(ts), max(ts), count(*) FROM bars"
                " WHERE dataset_id = ? GROUP BY symbol, interval",
                (ds_id,),
            ):
                entry = series.setdefault(symbol, {"intervals": {}, "funding": None})
                entry["intervals"][interval] = (_dt(first), _dt(last), count)  # type: ignore[index]
            for symbol, first, last, count in self._conn.execute(
                "SELECT symbol, min(ts), max(ts), count(*) FROM funding"
                " WHERE dataset_id = ? GROUP BY symbol",
                (ds_id,),
            ):
                series.setdefault(symbol, {"intervals": {}, "funding": None})[
                    "funding"
                ] = (_dt(first), _dt(last), count)
            out.append(
                DatasetInfo(
                    name=name,
                    source=source,
                    created_at=datetime.fromisoformat(created),
                    expires_at=datetime.fromisoformat(expires) if expires else None,
                    note=note,
                    series=series,
                )
            )
        return out

    # ---------------------------------------------------------------- bars

    def write_bars(
        self, dataset: str, symbol: str, interval: str, bars: Iterable[TimeBar]
    ) -> int:
        ds = self.dataset_id(dataset)
        rows = [
            (
                ds,
                symbol,
                interval,
                _ms(b.timestamp),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                str(b.volume),
                b.trade_count,
            )
            for b in bars
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def stored_intervals(self, dataset: str, symbol: str) -> list[str]:
        ds = self.find(dataset)
        if ds is None:
            return []
        rows = self._conn.execute(
            "SELECT DISTINCT interval FROM bars WHERE dataset_id = ? AND symbol = ?",
            (ds, symbol),
        ).fetchall()
        fetched = self._conn.execute(
            "SELECT DISTINCT kind FROM fetched WHERE dataset_id = ? AND symbol = ?"
            " AND kind LIKE 'bars_%'",
            (ds, symbol),
        ).fetchall()
        return sorted(
            {r[0] for r in rows} | {k[0].removeprefix("bars_") for k in fetched}
        )

    def raw_bars(
        self, dataset: str, symbol: str, interval: str, start: datetime, end: datetime
    ) -> list[TimeBar]:
        """Stored *interval* bars opening in [start, end]."""
        ds = self.find(dataset)
        if ds is None:
            return []
        size = INTERVAL_SECONDS[interval]
        return [
            TimeBar(
                symbol=symbol,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(lo),
                close=Decimal(c),
                volume=Decimal(v),
                trade_count=n,
                timestamp=_dt(ts),
                close_time=_dt(ts) + timedelta(seconds=size) - _MS,
                interval_seconds=size,
            )
            for ts, o, h, lo, c, v, n in self._conn.execute(
                "SELECT ts, open, high, low, close, volume, trades FROM bars"
                " WHERE dataset_id = ? AND symbol = ? AND interval = ?"
                " AND ts BETWEEN ? AND ?"
                " ORDER BY ts",
                (ds, symbol, interval, _ms(start), _ms(end)),
            )
        ]

    def count_bars(
        self, dataset: str, symbol: str, interval: str, start: datetime, end: datetime
    ) -> int:
        ds = self.find(dataset)
        if ds is None:
            return 0
        return int(
            self._conn.execute(
                "SELECT count(*) FROM bars"
                " WHERE dataset_id = ? AND symbol = ? AND interval = ?"
                " AND ts >= ? AND ts < ?",
                (ds, symbol, interval, _ms(start), _ms(end)),
            ).fetchone()[0]
        )

    # ------------------------------------------------------------- funding

    def write_funding(
        self, dataset: str, symbol: str, rates: Iterable[FundingRate]
    ) -> int:
        ds = self.dataset_id(dataset)
        rows = [(ds, symbol, _ms(r.timestamp), str(r.funding_rate)) for r in rates]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO funding VALUES (?, ?, ?, ?)", rows
            )
        return len(rows)

    def has_funding(self, dataset: str, symbol: str) -> bool:
        ds = self.find(dataset)
        return (
            ds is not None
            and self._conn.execute(
                "SELECT 1 FROM funding WHERE dataset_id = ? AND symbol = ? LIMIT 1",
                (ds, symbol),
            ).fetchone()
            is not None
        )

    def funding(
        self, dataset: str, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]:
        """Settlements in [start, end]; mark price 0 (filled from the bars)."""
        ds = self.find(dataset)
        if ds is None:
            return []
        return [
            FundingRate(
                symbol=symbol,
                funding_rate=Decimal(rate),
                mark_price=Decimal(0),
                timestamp=_dt(ts),
            )
            for ts, rate in self._conn.execute(
                "SELECT ts, rate FROM funding WHERE dataset_id = ? AND symbol = ?"
                " AND ts BETWEEN ? AND ? ORDER BY ts",
                (ds, symbol, _ms(start), _ms(end)),
            )
        ]

    def count_funding(
        self, dataset: str, symbol: str, start: datetime, end: datetime
    ) -> int:
        ds = self.find(dataset)
        if ds is None:
            return 0
        return int(
            self._conn.execute(
                "SELECT count(*) FROM funding"
                " WHERE dataset_id = ? AND symbol = ? AND ts >= ? AND ts < ?",
                (ds, symbol, _ms(start), _ms(end)),
            ).fetchone()[0]
        )

    # ------------------------------------------------------------- fetched

    def mark_fetched(
        self, symbol: str, kind: str, month: datetime, until: datetime, complete: bool
    ) -> None:
        ds = self.dataset_id(BYBIT, source=BYBIT)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO fetched VALUES (?, ?, ?, ?, ?, ?)",
                (ds, symbol, kind, month_key(month), until.isoformat(), int(complete)),
            )

    def fetched(
        self, symbol: str, kind: str, month: datetime
    ) -> tuple[datetime, bool] | None:
        """(until, complete) for a downloaded Bybit month, or ``None``."""
        ds = self.find(BYBIT)
        if ds is None:
            return None
        row = self._conn.execute(
            "SELECT until, complete FROM fetched WHERE dataset_id = ? AND symbol = ?"
            " AND kind = ? AND month = ?",
            (ds, symbol, kind, month_key(month)),
        ).fetchone()
        return None if row is None else (datetime.fromisoformat(row[0]), bool(row[1]))

    # --------------------------------------------------------------- rules

    def write_rules(self, dataset: str, exchange: str, rules: InstrumentRules) -> None:
        ds = self.dataset_id(dataset)

        def s(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ds,
                    exchange,
                    rules.symbol,
                    str(rules.qty_step),
                    s(rules.min_qty),
                    s(rules.max_market_qty),
                    s(rules.min_notional),
                ),
            )

    def rules(self, dataset: str) -> dict[tuple[str, str], InstrumentRules]:
        ds = self.find(dataset)
        if ds is None:
            return {}

        def d(v: str | None) -> Decimal | None:
            return None if v is None else Decimal(v)

        return {
            (exchange, symbol): InstrumentRules(
                symbol=symbol,
                qty_step=Decimal(step),
                min_qty=d(mn),
                max_market_qty=d(mx),
                min_notional=d(notional),
            )
            for exchange, symbol, step, mn, mx, notional in self._conn.execute(
                "SELECT exchange, symbol, qty_step, min_qty, max_market_qty,"
                " min_notional"
                " FROM rules WHERE dataset_id = ?",
                (ds,),
            )
        }
