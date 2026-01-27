"""
Flask-based webapp for displaying live strategy status.

Architecture:
- Data flows ONE direction: main event loop -> thread-safe store -> Flask
- Flask NEVER calls ib_async methods directly (ib_async is not thread-safe)
- The strategy's main event loop periodically updates the data store
- Flask only reads from the pre-computed snapshots
"""

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from flask import Flask, render_template, Response, jsonify

from ib_async import IB, Option, FuturesOption, Bag, Contract

if TYPE_CHECKING:
    from .ibkr_strategy import IbkrStrategy


@dataclass
class WebappDataStore:
    """Thread-safe data store for webapp data.

    All data is collected in the main event loop and stored here.
    Flask reads from this store, never from ib_async directly.
    """
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: str = "Stopped"
    _positions: list[dict] = field(default_factory=list)
    _orders: list[dict] = field(default_factory=list)
    _trades: list[dict] = field(default_factory=list)
    _chart_contracts: list[dict] = field(default_factory=list)
    _last_updated: str = ""

    def update(self, state: str, positions: list, orders: list,
               trades: list, chart_contracts: list) -> None:
        """Update all data atomically (called from main event loop)."""
        with self._lock:
            self._state = state
            self._positions = positions
            self._orders = orders
            self._trades = trades
            self._chart_contracts = chart_contracts
            self._last_updated = datetime.now().strftime("%H:%M:%S")

    def get_snapshot(self) -> dict:
        """Get a consistent snapshot of all data (called from Flask thread)."""
        with self._lock:
            return {
                "state": self._state,
                "positions": list(self._positions),
                "orders": list(self._orders),
                "trades": list(self._trades),
                "chart_contracts": list(self._chart_contracts),
                "last_updated": self._last_updated,
            }

    def get_state(self) -> str:
        """Get current state."""
        with self._lock:
            return self._state


class IbkrWebapp:
    """Flask-based webapp for displaying live strategy status."""

    def __init__(self, ib: IB, strategy: "IbkrStrategy"):
        self.ib = ib
        self.strategy = strategy
        self._data_store = WebappDataStore()
        self._contract_cache: dict[int, Any] = {}  # conId -> contract
        self._under_symbol_cache: dict[int, str] = {}  # conId -> underSymbol
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._app = self._create_app()

    def _create_app(self) -> Flask:
        """Create and configure the Flask application."""
        app = Flask(__name__, template_folder="templates")

        @app.route("/")
        def index():
            snapshot = self._data_store.get_snapshot()
            return render_template(
                "new_index.html",
                strategy_name=getattr(self.strategy, "name", "Unknown Strategy"),
                strategy_version=getattr(self.strategy, "version", "0.0.0"),
                state=snapshot["state"],
                positions=snapshot["positions"],
                orders=snapshot["orders"],
                trades=snapshot["trades"],
                chart_contracts=snapshot["chart_contracts"],
                last_updated=snapshot["last_updated"] or datetime.now().strftime("%H:%M:%S"),
            )

        @app.route("/events")
        def events():
            """Server-Sent Events endpoint for real-time updates."""
            def generate():
                while self._running:
                    # Just read from the thread-safe data store
                    snapshot = self._data_store.get_snapshot()
                    yield f"data: {json.dumps(snapshot)}\n\n"
                    time.sleep(2)

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

    # -------------------------------------------------------------------------
    # Data Collection Methods (called from main event loop only)
    # -------------------------------------------------------------------------

    async def collect_data(self) -> None:
        """Collect all data from ib_async and update the data store.

        This method MUST be called from the main event loop thread.
        It's safe to call ib_async methods here.
        """
        state = self._compute_state()
        positions = await self._collect_positions()
        orders = await self._collect_orders()
        trades = await self._collect_trades()
        chart_contracts = self._collect_chart_contracts()

        self._data_store.update(state, positions, orders, trades, chart_contracts)

    def _compute_state(self) -> str:
        """Compute current strategy state."""
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

    async def _get_under_symbol(self, contract: Contract) -> str:
        """Get the full underlying symbol for an option contract.

        For options/futures options, this returns the underlying symbol
        (e.g., 'ESH6' instead of just 'ES').
        """
        if contract.conId in self._under_symbol_cache:
            return self._under_symbol_cache[contract.conId]

        try:
            details_list = await self.ib.reqContractDetailsAsync(contract)
            if details_list:
                under_symbol = details_list[0].underSymbol or contract.symbol
                self._under_symbol_cache[contract.conId] = under_symbol
                return under_symbol
        except Exception:
            pass

        return contract.symbol

    async def _collect_positions(self) -> list[dict]:
        """Collect open positions with PnL and entry price."""
        positions = []
        try:
            portfolio_items = {
                p.contract.conId: p for p in self.ib.portfolio()
            }

            for pos in self.ib.positions():
                contract = pos.contract
                portfolio_item = portfolio_items.get(contract.conId)

                pnl = 0.0
                avg_cost = 0.0
                if portfolio_item:
                    pnl = portfolio_item.unrealizedPNL or 0.0
                    avg_cost = portfolio_item.averageCost or 0.0

                # Build condensed position description
                if isinstance(contract, (Option, FuturesOption)):
                    # Get full underlying symbol (e.g., "ESH6" instead of "ES")
                    under_symbol = await self._get_under_symbol(contract)
                    exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                    strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                    right = self._format_right(contract.right)
                    description = f"{exp_short} {under_symbol} {strike} {right}"
                else:
                    symbol = contract.localSymbol or contract.symbol
                    description = symbol

                qty = int(pos.position) if pos.position == int(pos.position) else pos.position
                # is_short determines if this is a credit (short) or debit (long) position
                is_short = qty < 0
                positions.append({
                    "quantity": qty,
                    "description": description,
                    "avg_cost": avg_cost,
                    "is_credit": is_short,  # Short positions were opened with a credit
                    "pnl": pnl,
                })
        except Exception:
            pass  # Return empty list on error

        return positions

    async def _format_contract_from_conid(self, con_id: int) -> str:
        """Format a contract description from conId using the cache."""
        cached = self._contract_cache.get(con_id)
        if cached:
            if isinstance(cached, (Option, FuturesOption)):
                under_symbol = await self._get_under_symbol(cached)
                exp_short = self._format_expiration(cached.lastTradeDateOrContractMonth)
                strike = int(cached.strike) if cached.strike == int(cached.strike) else cached.strike
                right = self._format_right(cached.right)
                return f"{exp_short} {under_symbol} {strike} {right}"
            else:
                return cached.localSymbol or cached.symbol or f"Contract {con_id}"
        return f"Contract {con_id}"

    def _abbreviate_status(self, status: str) -> str:
        """Abbreviate order status for display."""
        abbreviations = {
            "Submitted": "SUBM",
            "PreSubmitted": "PEND",
            "PendingSubmit": "PEND",
            "PendingCancel": "CANC",
            "Cancelled": "CANC",
            "Filled": "FILL",
            "Inactive": "INAC",
        }
        return abbreviations.get(status, status[:4].upper() if status else "")

    async def _collect_orders(self) -> list[dict]:
        """Collect open orders with combo legs grouped together."""
        orders = []
        try:
            for trade in self.ib.openTrades():
                contract = trade.contract
                order = trade.order

                # Cache the contract for later use
                if contract.conId:
                    self._contract_cache[contract.conId] = contract

                # Handle BAG (combo) contracts
                if isinstance(contract, Bag) and contract.comboLegs:
                    # This is a spread order - collect all legs into one order item
                    is_credit = order.action == "SELL"
                    limit_price = order.lmtPrice if order.lmtPrice else 0.0

                    legs = []
                    for leg in contract.comboLegs:
                        # Determine leg action prefix
                        # BTO = Buy to Open, STO = Sell to Open
                        # BTC = Buy to Close, STC = Sell to Close
                        if order.action == "BUY":
                            leg_action = "BTO" if leg.action == "BUY" else "STO"
                        else:
                            leg_action = "BTC" if leg.action == "BUY" else "STC"

                        leg_qty = leg.ratio
                        leg_desc = await self._format_contract_from_conid(leg.conId)

                        legs.append({
                            "action": leg_action,
                            "quantity": leg_qty,
                            "description": leg_desc,
                        })

                    orders.append({
                        "legs": legs,
                        "limit_price": limit_price,
                        "is_credit": is_credit,
                        "status": self._abbreviate_status(trade.orderStatus.status),
                        "is_combo": True,
                    })
                else:
                    # Regular single-leg order
                    if isinstance(contract, (Option, FuturesOption)):
                        under_symbol = await self._get_under_symbol(contract)
                        exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                        strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                        right = self._format_right(contract.right)
                        description = f"{exp_short} {under_symbol} {strike} {right}"
                    else:
                        symbol = contract.localSymbol or contract.symbol
                        description = symbol

                    limit_price = order.lmtPrice if order.lmtPrice else (order.auxPrice or 0.0)
                    qty = int(order.totalQuantity) if order.totalQuantity == int(order.totalQuantity) else order.totalQuantity

                    # Determine action: BTO/STO for opening, BTC/STC for closing
                    # For simplicity, use BTO for buys and STO for sells
                    action = "BTO" if order.action == "BUY" else "STO"
                    is_credit = order.action == "SELL"

                    orders.append({
                        "legs": [{
                            "action": action,
                            "quantity": qty,
                            "description": description,
                        }],
                        "limit_price": limit_price,
                        "is_credit": is_credit,
                        "status": self._abbreviate_status(trade.orderStatus.status),
                        "is_combo": False,
                    })
        except Exception:
            pass  # Return empty list on error

        return orders

    async def _collect_trades(self) -> list[dict]:
        """Collect filled orders using execution reports from IB.

        Note: IB only provides execution reports for the current trading day/session.
        Combo orders are grouped by order ID so all legs appear together.
        """
        trades = []

        try:
            # Request execution reports from IB - this populates ib.fills()
            # with any executions IB has for the current session/day
            await self.ib.reqExecutionsAsync()

            # Now get all fills (includes the ones we just requested)
            all_fills = self.ib.fills()

            # Group fills by order ID to combine combo legs
            fills_by_order: dict[int, list] = {}
            for fill in all_fills:
                order_id = fill.execution.orderId
                if order_id not in fills_by_order:
                    fills_by_order[order_id] = []
                fills_by_order[order_id].append(fill)

            for order_id, fills in fills_by_order.items():
                if not fills:
                    continue

                # Use the earliest fill time for sorting
                fills.sort(key=lambda f: f.time)
                first_fill = fills[0]
                fill_time = first_fill.time
                time_str = fill_time.strftime("%Y-%m-%d %H:%M:%S")

                legs = []
                total_credit = 0.0  # Track net credit/debit

                for fill in fills:
                    fill_contract = fill.contract
                    execution = fill.execution

                    # Cache fill contract
                    if fill_contract.conId:
                        self._contract_cache[fill_contract.conId] = fill_contract

                    # Build description
                    if isinstance(fill_contract, (Option, FuturesOption)):
                        under_symbol = await self._get_under_symbol(fill_contract)
                        exp_short = self._format_expiration(fill_contract.lastTradeDateOrContractMonth)
                        strike = int(fill_contract.strike) if fill_contract.strike == int(fill_contract.strike) else fill_contract.strike
                        right = self._format_right(fill_contract.right)
                        description = f"{exp_short} {under_symbol} {strike} {right}"
                    else:
                        symbol = fill_contract.localSymbol or fill_contract.symbol
                        description = symbol

                    qty = int(execution.shares) if execution.shares == int(execution.shares) else execution.shares
                    # Determine action: STO for sells, BTO for buys
                    action = "STO" if execution.side == "SLD" else "BTO"

                    # Track credit/debit: sells are credits, buys are debits
                    fill_value = execution.price * qty
                    if execution.side == "SLD":
                        total_credit += fill_value
                    else:
                        total_credit -= fill_value

                    legs.append({
                        "action": action,
                        "quantity": qty,
                        "description": description,
                    })

                # Determine if overall trade was a credit or debit
                is_credit = total_credit > 0
                fill_price = abs(total_credit) / max(sum(leg["quantity"] for leg in legs), 1)

                trades.append({
                    "time_sort": time_str,
                    "legs": legs,
                    "fill_price": fill_price,
                    "is_credit": is_credit,
                    "pnl": None,  # "Open" - we don't track realized PnL yet
                    "is_combo": len(legs) > 1,
                })
        except Exception:
            pass  # Return empty list on error

        trades.sort(key=lambda x: x["time_sort"], reverse=True)
        return trades

    def _collect_chart_contracts(self) -> list[dict]:
        """Collect contracts for charting from open positions."""
        contracts = []
        seen_con_ids = set()

        try:
            for pos in self.ib.positions():
                contract = pos.contract
                if contract.conId in seen_con_ids:
                    continue
                seen_con_ids.add(contract.conId)
                self._contract_cache[contract.conId] = contract

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
                })
        except Exception:
            pass  # Return empty list on error

        return contracts

    # -------------------------------------------------------------------------
    # Historical Data (uses thread-safe async call)
    # -------------------------------------------------------------------------

    def _get_historical_data(self, con_id: int) -> list[dict]:
        """Get historical price data for a contract.

        This is called from the Flask thread, so we must use
        asyncio.run_coroutine_threadsafe to run async code safely.
        """
        contract = self._contract_cache.get(con_id)
        if not contract:
            print(f"[Chart] No contract found for conId {con_id}")
            return []

        if self._loop is None:
            print("[Chart] No event loop available")
            return []

        # For options, try MIDPOINT first, then BID_ASK as fallback
        # For other instruments, use TRADES
        if isinstance(contract, (Option, FuturesOption)):
            what_to_show_options = ["MIDPOINT", "BID_ASK"]
        else:
            what_to_show_options = ["TRADES", "MIDPOINT"]

        for what_to_show in what_to_show_options:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr="1 D",
                        barSizeSetting="5 mins",
                        whatToShow=what_to_show,
                        useRTH=False,
                        formatDate=1,
                    ),
                    self._loop
                )
                bars = future.result(timeout=15.0)

                if bars:
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
            except Exception as e:
                print(f"[Chart] Error fetching {what_to_show} data for {con_id}: {e}")
                continue

        print(f"[Chart] No data available for conId {con_id}")
        return []

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self, port: int = 5000) -> None:
        """Start the Flask webapp in a daemon thread."""
        # Capture the current event loop for async calls from Flask thread
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        self._running = True

        def run_flask():
            # Suppress Flask's default logging for cleaner output
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)

            self._app.run(
                host="0.0.0.0",
                port=port,
                threaded=True,
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=run_flask, daemon=True)
        self._thread.start()
        print(f"Webapp started on http://localhost:{port}")

    def stop(self) -> None:
        """Stop the webapp."""
        self._running = False
