# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ibkrkit is a Python library for building trading bots using ib_async (the async wrapper for Interactive Brokers TWS API). It provides abstractions for creating trading strategies, streaming market data, and working with options chains.

## Development Setup

```bash
# Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.11+ and a running IBKR Gateway or TWS instance.

## Architecture

### Core Components

**IbkrStrategy** (`ibkr_strategy.py`): Base class for trading strategies. Supports two modes of operation:
- **Live mode**: `tick()` is called at the configured frequency
- **Sleep mode**: Strategy stays connected but `tick()` is not called

Subclass and implement:
- `on_strategy_init()`: Called once when strategy starts
- `on_day_start()`: Called each time live mode begins
- `tick()`: Called at `tick_freq_seconds` interval during live mode
- `on_day_end()`: Called each time live mode ends
- `on_strategy_shutdown()`: Called once when strategy stops

Run parameters:
- `day_start_time`: `time` object for when live mode starts each day (e.g., `time(9, 30)`)
- `day_stop_time`: `time` object for when live mode ends each day (e.g., `time(16, 0)`)
- `tick_freq_seconds`: Tick frequency in seconds (default: 5)

If both `day_start_time` and `day_stop_time` are `None`, strategy runs in "always live" mode.

Provides bracket order placement with OCA (One-Cancels-All) groups and event handlers for order fills/cancellations.

**IbkrDataStream** (`ibkr_data_stream.py`): Wraps ib_async market data subscriptions into a pandas DataFrame. Created via async factory `IbkrDataStream.create()`. Handles timezone conversion (US/Eastern with DST correction).

**IbkrOptionChain** (`ibkr_option_chain.py`): Manages options chain data with automatic refresh. Created via async factory `IbkrOptionChain.create()`. Filters by strike distance and DTE. Provides `find_best_strike_by_delta()` for delta-targeted strike selection.

**IbkrLogger** (`ibkr_logger.py`): SQLite-based trade logger (deprecated - prefer ib_async.util.logToFile).

### Key Patterns

- All components use ib_async's async/await patterns with nest_asyncio for nested event loop support
- Factory methods (`create()`) handle async initialization since `__init__` cannot be async
- Market data uses streaming subscriptions rather than snapshots for real-time updates
- Options chain maintains a dict of `(expiration, strike, right)` -> Ticker for efficient lookups

### Connection

Default connection parameters:
- Host: 127.0.0.1
- Port: 7496 (TWS) or 4004 (Gateway)
- Use `wait_for_ibkr_ready()` utility for connection retry logic
