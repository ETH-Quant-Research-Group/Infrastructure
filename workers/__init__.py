"""Entry-point workers for the distributed NATS architecture.

Each worker runs as a separate process:

- ``feed_server``: connects to Binance and publishes market data to NATS.
- ``strategy_worker``: subscribes to market data, runs one strategy, and
  publishes TargetPositions to ``signals.targets.<strategy_id>``.
- ``consolidator_worker``: subscribes to all TargetPositions, nets them, and
  places orders via the configured broker.
"""
