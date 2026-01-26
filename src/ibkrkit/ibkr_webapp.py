import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from flask import Flask, render_template
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

        return app

    def _get_state(self) -> str:
        """Get current strategy state."""
        if not self.ib.isConnected():
            return "Stopped"
        if self.strategy.is_live:
            return "Live"
        return "Sleep"

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

            # Extract option-specific fields
            strike = ""
            expiration = ""
            if isinstance(contract, (Option, FuturesOption)):
                strike = f"{contract.strike}{contract.right}"
                expiration = contract.lastTradeDateOrContractMonth

            positions.append({
                "time_opened": "N/A",  # IBKR doesn't provide this
                "symbol": contract.symbol or contract.localSymbol,
                "strike": strike,
                "expiration": expiration,
                "quantity": pos.position,
                "avg_cost": f"{pos.avgCost:.2f}" if pos.avgCost else "N/A",
                "pnl": pnl,
            })

        return positions

    def _get_open_orders(self) -> list[dict]:
        """Get open orders, grouped by spread (via ocaGroup or parentId)."""
        orders = []

        for trade in self.ib.openTrades():
            contract = trade.contract
            order = trade.order

            # Extract option-specific fields
            strike = ""
            expiration = ""
            if isinstance(contract, (Option, FuturesOption)):
                strike = f"{contract.strike}{contract.right}"
                expiration = contract.lastTradeDateOrContractMonth

            # Determine limit price
            limit_price = ""
            if order.lmtPrice:
                limit_price = f"{order.lmtPrice:.2f}"
            elif order.auxPrice:
                limit_price = f"{order.auxPrice:.2f} (stop)"

            # Use log time if available
            time_placed = "N/A"
            if trade.log:
                time_placed = trade.log[0].time.strftime("%Y-%m-%d %H:%M:%S")

            orders.append({
                "time_placed": time_placed,
                "symbol": contract.symbol or contract.localSymbol,
                "strike": strike,
                "expiration": expiration,
                "action": order.action,
                "quantity": order.totalQuantity,
                "limit_price": limit_price,
                "status": trade.orderStatus.status,
                "oca_group": order.ocaGroup,
                "parent_id": order.parentId,
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

                # Extract option-specific fields
                strike = ""
                expiration = ""
                if isinstance(contract, (Option, FuturesOption)):
                    strike = f"{contract.strike}{contract.right}"
                    expiration = contract.lastTradeDateOrContractMonth

                trades.append({
                    "time_executed": fill.time.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": contract.symbol or contract.localSymbol,
                    "strike": strike,
                    "expiration": expiration,
                    "action": execution.side,
                    "quantity": execution.shares,
                    "price": f"{execution.price:.2f}",
                    "oca_group": trade.order.ocaGroup if trade.order else "",
                })

        # Sort by time, most recent first
        trades.sort(key=lambda x: x["time_executed"], reverse=True)
        return trades

    def start(self, port: int = 5000) -> None:
        """Start the Flask webapp in a daemon thread."""
        def run_flask():
            self._app.run(
                host="0.0.0.0",
                port=port,
                threaded=False,
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=run_flask, daemon=True)
        self._thread.start()
        print(f"Webapp started on http://localhost:{port}")
