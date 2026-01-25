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

**IbkrStrategy** (`ibkr_strategy.py`): Base class for trading strategies. Subclass and implement:
- `on_start()`: Called once when strategy begins
- `tick()`: Called every 5 seconds during the trading day
- `on_stop()`: Called when strategy ends

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
