import json
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from flask import Flask, render_template, Response

from ib_async import IB, Option, FuturesOption

if TYPE_CHECKING:
    from .ibkr_strategy import IbkrStrategy


class IbkrWebapp:
    """Flask-based webapp for displaying live strategy status."""

    def __init__(self, ib: IB, strategy: "IbkrStrategy"):
        self.ib = ib
        self.strategy = strategy
        self._app = self._create_app()
        self._thread = None

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
            symbol = contract.localSymbol or contract.symbol
            if isinstance(contract, (Option, FuturesOption)):
                exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                right = self._format_right(contract.right)
                description = f"{exp_short} {symbol} {strike} {right}"
            else:
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
            symbol = contract.localSymbol or contract.symbol
            if isinstance(contract, (Option, FuturesOption)):
                exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                right = self._format_right(contract.right)
                description = f"{exp_short} {symbol} {strike} {right}"
            else:
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
        """Get trades from the past 48 hours."""
        trades = []
        cutoff = datetime.now() - timedelta(hours=48)

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
                symbol = contract.localSymbol or contract.symbol
                if isinstance(contract, (Option, FuturesOption)):
                    exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                    strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                    right = self._format_right(contract.right)
                    description = f"{exp_short} {symbol} {strike} {right}"
                else:
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

    def start(self, port: int = 5000) -> None:
        """Start the Flask webapp in a daemon thread."""
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
