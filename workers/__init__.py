"""Entry-point workers for the distributed NATS architecture.

Each worker runs as a separate process:

- ``feed_server``: connects to Binance and publishes market data to NATS.
- ``strategy_worker``: subscribes to market data, runs one strategy, and
  publishes TargetPositions to ``signals.targets.<strategy_id>``.
- ``consolidator_worker``: subscribes to all TargetPositions, places one
  tagged order per signal via the configured broker, and reports the real
  fills back.

Not a long-running worker: ``recover_ledger`` rebuilds the consolidator's
ledger from its snapshots + Bybit, run by hand.
"""
