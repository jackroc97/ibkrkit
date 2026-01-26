import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from flask import Flask, render_template, Response, jsonify

from ib_async import IB, Option, FuturesOption, Future, Stock

if TYPE_CHECKING:
    from .ibkr_strategy import IbkrStrategy


class IbkrWebapp:
    """Flask-based webapp for displaying live strategy status."""

    def __init__(self, ib: IB, strategy: "IbkrStrategy"):
        self.ib = ib
        self.strategy = strategy
        self._app = self._create_app()
        self._thread = None
        self._contract_cache = {}  # conId -> contract for underlying lookups
        self._loop = None  # Store reference to the main event loop

    def _create_app(self) -> Flask:
        """Create and configure the Flask application."""
        app = Flask(__name__, template_folder="templates")

        @app.route("/")
        def index():
            return render_template(
                "index.html",
                strategy_name=getattr(self.strategy, "name", "Unknown Strategy"),
                strategy_version=getattr(self.strategy, "version", "0.0.0"),
                state=self._get_state(),
                positions=self._get_positions(),
                orders=self._get_open_orders(),
                trades=self._get_recent_trades(),
                chart_contracts=self._get_chart_contracts(),
                last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

        @app.route("/events")
        def events():
            """Server-Sent Events endpoint for real-time updates."""
            def generate():
                while True:
                    data = {
                        "state": self._get_state(),
                        "positions": self._get_positions(),
                        "orders": self._get_open_orders(),
                        "trades": self._get_recent_trades(),
                        "chart_contracts": self._get_chart_contracts(),
                        "last_updated": datetime.now().strftime("%H:%M:%S"),
                    }
                    yield f"data: {json.dumps(data)}\n\n"
                    time.sleep(2)  # Update every 2 seconds

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
            )

        @app.route("/chart_data/<int:con_id>")
        def chart_data(con_id):
            """Get historical price data for a contract."""
            data = self._get_historical_data(con_id)
            return jsonify(data)

        return app

    def _get_state(self) -> str:
        """Get current strategy state."""
        if not self.ib.isConnected():
            return "Stopped"
        if self.strategy.is_live:
            return "Live"
        return "Sleep"

    def _format_expiration(self, expiration: str) -> str:
        """Format expiration date like 'Jan 27' from '20260127' format."""
        if not expiration:
            return ""
        try:
            dt = datetime.strptime(expiration, "%Y%m%d")
            return dt.strftime("%b %d")
        except ValueError:
            return expiration

    def _format_right(self, right: str) -> str:
        """Format option right as PUT or CALL."""
        if right == "P":
            return "PUT"
        elif right == "C":
            return "CALL"
        return right

    def _get_positions(self) -> list[dict]:
        """Get open positions with PnL, grouped by spread."""
        positions = []
        portfolio_items = {
            (p.contract.conId,): p for p in self.ib.portfolio()
        }

        for pos in self.ib.positions():
            contract = pos.contract
            portfolio_item = portfolio_items.get((contract.conId,))

            pnl = 0.0
            if portfolio_item:
                pnl = portfolio_item.unrealizedPNL or 0.0

            # Build condensed position description
            # For options, use the underlying symbol to avoid duplicating strike/right
            if isinstance(contract, (Option, FuturesOption)):
                symbol = contract.symbol
                exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                right = self._format_right(contract.right)
                description = f"{exp_short} {symbol} {strike} {right}"
            else:
                symbol = contract.localSymbol or contract.symbol
                description = symbol

            qty = int(pos.position) if pos.position == int(pos.position) else pos.position
            positions.append({
                "quantity": qty,
                "description": description,
                "pnl": pnl,
            })

        return positions

    def _get_open_orders(self) -> list[dict]:
        """Get open orders, grouped by spread (via ocaGroup or parentId)."""
        orders = []

        for trade in self.ib.openTrades():
            contract = trade.contract
            order = trade.order

            # Build condensed order description
            # For options, use the underlying symbol to avoid duplicating strike/right
            if isinstance(contract, (Option, FuturesOption)):
                symbol = contract.symbol
                exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                right = self._format_right(contract.right)
                description = f"{exp_short} {symbol} {strike} {right}"
            else:
                symbol = contract.localSymbol or contract.symbol
                description = symbol

            # Determine limit price
            limit_price = 0.0
            if order.lmtPrice:
                limit_price = order.lmtPrice
            elif order.auxPrice:
                limit_price = order.auxPrice

            qty = int(order.totalQuantity) if order.totalQuantity == int(order.totalQuantity) else order.totalQuantity
            # Prefix with action (BUY/SELL)
            action_prefix = "+" if order.action == "BUY" else "-"

            orders.append({
                "quantity": f"{action_prefix}{qty}",
                "description": description,
                "limit_price": limit_price,
                "status": trade.orderStatus.status,
            })

        return orders

    def _get_recent_trades(self) -> list[dict]:
        """Get trades from the past month (filtering done client-side)."""
        trades = []
        cutoff = datetime.now() - timedelta(days=30)

        for trade in self.ib.trades():
            # Only include trades with fills
            if not trade.fills:
                continue

            for fill in trade.fills:
                # Filter by time
                fill_time = fill.time
                if fill_time.tzinfo:
                    fill_time = fill_time.replace(tzinfo=None)
                if fill_time < cutoff:
                    continue

                contract = fill.contract
                execution = fill.execution

                # Build condensed trade description
                # For options, use the underlying symbol to avoid duplicating strike/right
                if isinstance(contract, (Option, FuturesOption)):
                    symbol = contract.symbol
                    exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                    strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                    right = self._format_right(contract.right)
                    description = f"{exp_short} {symbol} {strike} {right}"
                else:
                    symbol = contract.localSymbol or contract.symbol
                    description = symbol

                qty = int(execution.shares) if execution.shares == int(execution.shares) else execution.shares
                # Prefix with action (BOT/SLD shown as +/-)
                action_prefix = "+" if execution.side == "BOT" else "-"

                # Format time as just time portion for recent trades
                time_str = fill.time.strftime("%H:%M")

                trades.append({
                    "time_executed": time_str,
                    "time_sort": fill.time.strftime("%Y-%m-%d %H:%M:%S"),
                    "quantity": f"{action_prefix}{qty}",
                    "description": description,
                    "price": execution.price,
                })

        # Sort by time, most recent first
        trades.sort(key=lambda x: x["time_sort"], reverse=True)
        return trades

    def _get_chart_contracts(self) -> list[dict]:
        """Get contracts for charting from open positions, including underlyings."""
        contracts = []
        seen_con_ids = set()

        for pos in self.ib.positions():
            contract = pos.contract
            if contract.conId in seen_con_ids:
                continue
            seen_con_ids.add(contract.conId)
            self._contract_cache[contract.conId] = contract

            # Build label for the contract
            if isinstance(contract, (Option, FuturesOption)):
                exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                right = self._format_right(contract.right)
                label = f"{contract.symbol} {exp_short} {strike} {right}"
            else:
                label = contract.localSymbol or contract.symbol

            contracts.append({
                "conId": contract.conId,
                "label": label,
                "type": "position",
            })

            # Add underlying contract if this is an option
            if isinstance(contract, Option):
                # Create underlying stock contract
                underlying = Stock(contract.symbol, "SMART", contract.currency)
                qualified = self._run_async(self.ib.qualifyContractsAsync(underlying))
                if qualified:
                    underlying = qualified[0]
                    if underlying.conId not in seen_con_ids:
                        seen_con_ids.add(underlying.conId)
                        self._contract_cache[underlying.conId] = underlying
                        contracts.append({
                            "conId": underlying.conId,
                            "label": underlying.symbol,
                            "type": "underlying",
                        })
            elif isinstance(contract, FuturesOption):
                # Create underlying futures contract
                underlying = Future(
                    symbol=contract.symbol,
                    lastTradeDateOrContractMonth=contract.lastTradeDateOrContractMonth,
                    exchange=contract.exchange,
                    currency=contract.currency,
                )
                qualified = self._run_async(self.ib.qualifyContractsAsync(underlying))
                if qualified:
                    underlying = qualified[0]
                    if underlying.conId not in seen_con_ids:
                        seen_con_ids.add(underlying.conId)
                        self._contract_cache[underlying.conId] = underlying
                        contracts.append({
                            "conId": underlying.conId,
                            "label": underlying.localSymbol or underlying.symbol,
                            "type": "underlying",
                        })

        return contracts

    def _get_historical_data(self, con_id: int) -> list[dict]:
        """Get historical price data for a contract."""
        # Find the contract from cache or positions
        contract = self._contract_cache.get(con_id)
        if not contract:
            for pos in self.ib.positions():
                if pos.contract.conId == con_id:
                    contract = pos.contract
                    break

        if not contract:
            return []

        try:
            bars = self._run_async(self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
            ))

            if not bars:
                return []

            return [
                {
                    "time": int(bar.date.timestamp()),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                }
                for bar in bars
            ]
        except Exception:
            return []

    def _run_async(self, coro):
        """Run an async coroutine from the Flask thread safely."""
        if self._loop is None:
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=5.0)
        except Exception:
            return None

    def start(self, port: int = 5000) -> None:
        """Start the Flask webapp in a daemon thread."""
        # Capture the current event loop for async calls from Flask thread
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        def run_flask():
            self._app.run(
                host="0.0.0.0",
                port=port,
                threaded=True,  # Enable threading for SSE support
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=run_flask, daemon=True)
        self._thread.start()
        print(f"Webapp started on http://localhost:{port}")
