"""
Flask-based webapp for displaying live trading status.

Architecture:
- IbkrWebapp runs standalone with its own IB connection
- Data flows ONE direction: main event loop -> thread-safe store -> Flask
- Flask NEVER calls ib_async methods directly (ib_async is not thread-safe)
- The webapp's own data collection loop periodically updates the data store
- Flask only reads from the pre-computed snapshots
"""

import asyncio
import json
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any

from flask import Flask, render_template, Response, jsonify

from ib_async import IB, Option, FuturesOption, Future, Bag, Contract, ContractDetails


@dataclass
class WebappDataStore:
    """Thread-safe data store for webapp data.

    All data is collected in the main event loop and stored here.
    Flask reads from this store, never from ib_async directly.
    """
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: str = "Disconnected"
    _positions: list[dict] = field(default_factory=list)
    _orders: list[dict] = field(default_factory=list)
    _trades: list[dict] = field(default_factory=list)
    _chart_contracts: list[dict] = field(default_factory=list)
    _account_summary: dict = field(default_factory=dict)
    _last_updated: str = ""

    def update(self, state: str, positions: list, orders: list,
               trades: list, chart_contracts: list, account_summary: dict = None) -> None:
        """Update all data atomically (called from main event loop)."""
        with self._lock:
            self._state = state
            self._positions = positions
            self._orders = orders
            self._trades = trades
            self._chart_contracts = chart_contracts
            self._account_summary = account_summary or {}
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
                "account_summary": dict(self._account_summary),
                "last_updated": self._last_updated,
            }

    def get_state(self) -> str:
        """Get current state."""
        with self._lock:
            return self._state


class IbkrWebapp:
    """Flask-based webapp for displaying live trading status.

    Runs standalone with its own IB connection. Can be used independently
    of IbkrStrategy to monitor positions, orders, and trades.
    """

    def __init__(
        self,
        name: str = "IbkrWebapp",
        version: str = "1.0.0",
    ):
        """Initialize the webapp.

        Args:
            name: Display name shown in the webapp header
            version: Version string shown in the webapp header
        """
        self._name = name
        self._version = version

        # IB connection - created when start() is called
        self.ib: IB | None = None

        self._data_store = WebappDataStore()
        self._contract_cache: dict[int, Any] = {}  # conId -> contract
        self._under_symbol_cache: dict[int, str] = {}  # conId -> underSymbol
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._data_collection_task: asyncio.Task | None = None
        self._running = False
        self._app = self._create_app()

        # Bar data for charting (thread-safe access required)
        self._bar_data_lock = threading.Lock()
        self._bar_data: dict[int, list[dict]] = {}  # conId -> list of OHLC bars
        self._bar_subscriptions: dict[int, Any] = {}  # conId -> BarDataList from reqHistoricalData
        self._bar_aggregation: dict[int, dict] = {}  # conId -> current minute aggregation state

        # Tickers for bid/ask data (separate from bar subscriptions)
        self._bid_ask_tickers: dict[int, Any] = {}  # conId -> Ticker for bid/ask access

        # Breakeven data for chart price lines (thread-safe access required)
        self._breakevens: dict[int, list[dict]] = {}  # underlying conId -> list of breakeven info

        # Day start/stop times for sleeping state (set via start())
        self._day_start_time: time | None = None
        self._day_stop_time: time | None = None

    def _create_app(self) -> Flask:
        """Create and configure the Flask application."""
        app = Flask(__name__, template_folder="templates")

        @app.route("/")
        def index():
            snapshot = self._data_store.get_snapshot()
            return render_template(
                "index.html",
                strategy_name=self._name,
                strategy_version=self._version,
                state=snapshot["state"],
                positions=snapshot["positions"],
                orders=snapshot["orders"],
                trades=snapshot["trades"],
                chart_contracts=snapshot["chart_contracts"],
                account_summary=snapshot["account_summary"],
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
                    time_module.sleep(2)

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
            """Get bar data for a contract from the live stream."""
            data = self._get_bar_data(con_id)
            return jsonify(data)

        @app.route("/breakevens/<int:underlying_con_id>")
        def breakevens(underlying_con_id):
            """Get breakeven price lines for an underlying contract."""
            with self._bar_data_lock:
                data = list(self._breakevens.get(underlying_con_id, []))
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
        account_summary = self._collect_account_summary()
        positions = await self._collect_positions()
        orders = await self._collect_orders()
        trades = await self._collect_trades()
        chart_contracts = await self._collect_chart_contracts_and_update_streams()

        # Calculate breakevens for options positions (for chart price lines)
        await self._collect_breakevens()

        self._data_store.update(state, positions, orders, trades, chart_contracts, account_summary)

    def _compute_state(self) -> str:
        """Compute current connection state."""
        if self.ib is None or not self.ib.isConnected():
            return "Disconnected"

        # Check if we're in the sleeping period (outside day_start_time to day_stop_time)
        if self._day_start_time is not None and self._day_stop_time is not None:
            now = datetime.now().time()
            # Handle normal case (start < stop, e.g., 9:30 to 16:00)
            if self._day_start_time <= self._day_stop_time:
                if not (self._day_start_time <= now <= self._day_stop_time):
                    return "Sleeping"
            else:
                # Handle overnight case (start > stop, e.g., 18:00 to 05:00)
                if not (now >= self._day_start_time or now <= self._day_stop_time):
                    return "Sleeping"

        return "Connected"

    def _collect_account_summary(self) -> dict:
        """Collect account summary values from IB.

        Returns:
            Dict with account values for display.
        """
        summary = {
            "account_number": "",
            "net_liquidation": 0.0,
            "buying_power": 0.0,
            "init_margin": 0.0,
            "maint_margin": 0.0,
            "excess_liquidity": 0.0,
        }

        try:
            for av in self.ib.accountValues():
                if av.tag == "AccountCode":
                    summary["account_number"] = av.value
                elif av.tag == "NetLiquidation" and av.currency == "USD":
                    summary["net_liquidation"] = float(av.value)
                elif av.tag == "BuyingPower" and av.currency == "USD":
                    summary["buying_power"] = float(av.value)
                elif av.tag == "InitMarginReq" and av.currency == "USD":
                    summary["init_margin"] = float(av.value)
                elif av.tag == "MaintMarginReq" and av.currency == "USD":
                    summary["maint_margin"] = float(av.value)
                elif av.tag == "ExcessLiquidity" and av.currency == "USD":
                    summary["excess_liquidity"] = float(av.value)
        except Exception:
            pass

        return summary

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

    def _format_contract_month(self, contract_month: str) -> str:
        """Format contract month from YYYYMM to 'Month Year' format.

        Example: '202603' -> 'March 2026'
        """
        if not contract_month or len(contract_month) < 6:
            return contract_month or ""
        try:
            dt = datetime.strptime(contract_month[:6], "%Y%m")
            return dt.strftime("%B %Y")
        except ValueError:
            return contract_month

    async def _format_underlying_label(self, contract: Contract) -> str:
        """Format a label for an underlying contract (futures, stocks, etc.).

        For futures: <localSymbol> <longName> <contractMonth>
        Where contractMonth is formatted as full month name + year.
        """
        try:
            details_list = await self.ib.reqContractDetailsAsync(contract)
            if details_list:
                details = details_list[0]
                local_symbol = details.contract.localSymbol or contract.localSymbol or contract.symbol
                long_name = details.longName or ""
                contract_month = details.contractMonth or ""

                if contract_month:
                    formatted_month = self._format_contract_month(contract_month)
                    return f"{local_symbol} {long_name} {formatted_month}".strip()
                elif long_name:
                    return f"{local_symbol} {long_name}".strip()
                else:
                    return local_symbol
        except Exception:
            pass

        return contract.localSymbol or contract.symbol

    async def _get_underlying_contract(self, contract: Contract) -> Contract | None:
        """Get the underlying contract for an option/futures option.

        Returns the qualified underlying contract, or None if not found.
        """
        if not isinstance(contract, (Option, FuturesOption)):
            return None

        try:
            details_list = await self.ib.reqContractDetailsAsync(contract)
            if not details_list:
                return None

            details = details_list[0]
            under_con_id = details.underConId

            if not under_con_id:
                return None

            # Create and qualify the underlying contract
            if isinstance(contract, FuturesOption):
                # Underlying is a Future
                under_contract = Future(conId=under_con_id)
            else:
                # Underlying is typically Stock or Index
                under_contract = Contract(conId=under_con_id)

            qualified = await self.ib.qualifyContractsAsync(under_contract)
            if qualified:
                return qualified[0]

        except Exception as e:
            print(f"[Chart] Error getting underlying for {contract.localSymbol}: {e}")

        return None

    def _calculate_breakevens(self, options: list[dict]) -> list[dict]:
        """Calculate breakeven prices for a group of options (same underlying, same expiration).

        Args:
            options: List of dicts with keys: strike, right ('C'/'P'), quantity, avg_cost,
                     expiration, description

        Returns:
            List of breakeven dicts with keys: price, label, contracts
        """
        if not options:
            return []

        calls = [o for o in options if o['right'] == 'C']
        puts = [o for o in options if o['right'] == 'P']

        # Calculate premiums (positive = debit/paid, negative = credit/received)
        total_premium = sum(o['quantity'] * o['avg_cost'] for o in options)
        call_premium = sum(o['quantity'] * o['avg_cost'] for o in calls)
        put_premium = sum(o['quantity'] * o['avg_cost'] for o in puts)

        # Check if we have mixed long/short within calls or puts
        call_has_long = any(o['quantity'] > 0 for o in calls)
        call_has_short = any(o['quantity'] < 0 for o in calls)
        put_has_long = any(o['quantity'] > 0 for o in puts)
        put_has_short = any(o['quantity'] < 0 for o in puts)

        breakevens = []

        def format_exp_compact(expiration: str) -> str:
            """Format expiration as '27JAN' from '20260127'."""
            if not expiration or len(expiration) < 8:
                return expiration or ""
            try:
                dt = datetime.strptime(expiration, "%Y%m%d")
                return dt.strftime("%d%b").upper()  # e.g., "27JAN"
            except ValueError:
                return expiration

        def make_label(contracts: list[dict], is_call_based: bool, premium: float) -> str:
            """Build label like '▲ -1x27JAN6900P/1x27JAN6850P ▲'.

            Args:
                contracts: List of option dicts with quantity, expiration, strike, right
                is_call_based: True if this BE is from calls, False if from puts
                premium: The net premium (positive = debit, negative = credit)
            """
            # Determine arrow direction based on option type and premium
            # Call-based BE: debit (paid) means profit above BE (▲), credit means profit below (▼)
            # Put-based BE: debit (paid) means profit below BE (▼), credit means profit above (▲)
            if is_call_based:
                arrow = "▲" if premium > 0 else "▼"
            else:
                arrow = "▼" if premium > 0 else "▲"

            # Sort contracts by decreasing strike
            sorted_contracts = sorted(contracts, key=lambda c: c['strike'], reverse=True)

            # Format each contract as qty x date month strike right
            parts = []
            for c in sorted_contracts:
                qty = int(c['quantity']) if c['quantity'] == int(c['quantity']) else c['quantity']
                exp_compact = format_exp_compact(c['expiration'])
                strike = int(c['strike']) if c['strike'] == int(c['strike']) else c['strike']
                right_short = c['right']
                parts.append(f"{qty}x{exp_compact}{strike}{right_short}")

            return f"{arrow} {'/'.join(parts)} {arrow}"

        # Breakeven formulas (works for both credit and debit spreads):
        # - Call spread BE = Lowest strike + |net premium|
        # - Put spread BE = Highest strike - |net premium|
        #
        # For straddles/strangles, we use total premium for both sides.
        # For iron condors or separate spreads, we calculate each side independently.

        if calls and puts:
            # Mixed position - could be straddle/strangle or iron condor
            calls_same_dir = call_has_long != call_has_short  # XOR - all long or all short
            puts_same_dir = put_has_long != put_has_short

            if calls_same_dir and puts_same_dir and not (call_has_long != put_has_long):
                # Straddle/strangle type (both sides same direction) - use total premium
                # Call side BE
                call_anchor = min(o['strike'] for o in calls)
                breakevens.append({
                    'price': call_anchor + abs(total_premium),
                    'label': make_label(calls, is_call_based=True, premium=total_premium),
                })
                # Put side BE
                put_anchor = max(o['strike'] for o in puts)
                breakevens.append({
                    'price': put_anchor - abs(total_premium),
                    'label': make_label(puts, is_call_based=False, premium=total_premium),
                })
            else:
                # Iron condor or mixed - calculate each side independently
                if calls:
                    call_anchor = min(o['strike'] for o in calls)
                    breakevens.append({
                        'price': call_anchor + abs(call_premium),
                        'label': make_label(calls, is_call_based=True, premium=call_premium),
                    })
                if puts:
                    put_anchor = max(o['strike'] for o in puts)
                    breakevens.append({
                        'price': put_anchor - abs(put_premium),
                        'label': make_label(puts, is_call_based=False, premium=put_premium),
                    })

        elif calls:
            # Call spread: BE = Lowest strike + |net premium|
            call_anchor = min(o['strike'] for o in calls)
            breakevens.append({
                'price': call_anchor + abs(call_premium),
                'label': make_label(calls, is_call_based=True, premium=call_premium),
            })

        elif puts:
            # Put spread: BE = Highest strike - |net premium|
            put_anchor = max(o['strike'] for o in puts)
            breakevens.append({
                'price': put_anchor - abs(put_premium),
                'label': make_label(puts, is_call_based=False, premium=put_premium),
            })

        return breakevens

    async def _collect_breakevens(self) -> None:
        """Collect and calculate breakevens for all options positions.

        Groups options by underlying conId and expiration, calculates breakevens,
        and stores them keyed by underlying conId.
        """
        # Group options positions by underlying conId and expiration
        # Structure: {underlying_conId: {expiration: [option_info, ...]}}
        options_by_underlying: dict[int, dict[str, list[dict]]] = {}

        try:
            portfolio_items = {p.contract.conId: p for p in self.ib.portfolio()}

            for pos in self.ib.positions():
                contract = pos.contract
                if not isinstance(contract, (Option, FuturesOption)):
                    continue

                # Get underlying conId
                underlying = await self._get_underlying_contract(contract)
                if not underlying or not underlying.conId:
                    continue

                under_con_id = underlying.conId
                expiration = contract.lastTradeDateOrContractMonth

                # Get avg_cost from portfolio
                portfolio_item = portfolio_items.get(contract.conId)
                avg_cost = 0.0
                if portfolio_item:
                    avg_cost = portfolio_item.averageCost or 0.0
                    # Divide out multiplier for consistency
                    if contract.multiplier:
                        multiplier = float(contract.multiplier)
                        if multiplier > 0:
                            avg_cost = avg_cost / multiplier

                option_info = {
                    'strike': contract.strike,
                    'right': contract.right,
                    'quantity': pos.position,
                    'avg_cost': avg_cost,
                    'expiration': expiration,
                    'description': contract.localSymbol or f"{contract.symbol} {expiration} {contract.strike} {contract.right}",
                }

                if under_con_id not in options_by_underlying:
                    options_by_underlying[under_con_id] = {}
                if expiration not in options_by_underlying[under_con_id]:
                    options_by_underlying[under_con_id][expiration] = []
                options_by_underlying[under_con_id][expiration].append(option_info)

            # Calculate breakevens for each underlying
            new_breakevens: dict[int, list[dict]] = {}
            for under_con_id, expirations in options_by_underlying.items():
                underlying_breakevens = []
                for expiration, options in expirations.items():
                    bes = self._calculate_breakevens(options)
                    underlying_breakevens.extend(bes)
                if underlying_breakevens:
                    new_breakevens[under_con_id] = underlying_breakevens

            # Update thread-safe storage
            with self._bar_data_lock:
                self._breakevens = new_breakevens

        except Exception as e:
            import traceback
            print(f"[Breakevens] Error collecting breakevens: {e}")
            traceback.print_exc()

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
                    # For options, avg_cost includes the multiplier - divide it out for consistency
                    if isinstance(contract, (Option, FuturesOption)) and contract.multiplier:
                        multiplier = float(contract.multiplier)
                        if multiplier > 0:
                            avg_cost = avg_cost / multiplier

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

    def _get_order_action(self, order_side: str, position_qty: float) -> str:
        """Determine order action (BTO/STO/BTC/STC) based on order side and current position.

        Args:
            order_side: "BUY" or "SELL"
            position_qty: Current position quantity (positive=long, negative=short, 0=flat)

        Returns:
            Action string: BTO, STO, BTC, or STC
        """
        if order_side == "BUY":
            # Buying: if we're short, this closes; otherwise it opens
            return "BTC" if position_qty < 0 else "BTO"
        else:
            # Selling: if we're long, this closes; otherwise it opens
            return "STC" if position_qty > 0 else "STO"

    def _get_contract_bid_ask(self, con_id: int) -> tuple[float | None, float | None]:
        """Get the current bid/ask for a contract from its ticker.

        Returns:
            Tuple of (bid, ask), either may be None if not available.
        """
        ticker = self._bid_ask_tickers.get(con_id)
        if not ticker:
            return None, None

        bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        return bid, ask

    def _calculate_combo_bid_ask(
        self, legs: list[dict]
    ) -> tuple[float | None, float | None]:
        """Calculate the combo bid and ask prices from individual leg bid/asks.

        The combo bid is what we'd receive if we SELL the combo.
        The combo ask is what we'd pay if we BUY the combo.

        For each leg based on its action in the combo definition:
        - BUY leg: buying the combo means we buy this leg (pay ask), selling means we sell (receive bid)
        - SELL leg: buying the combo means we sell this leg (receive bid), selling means we buy (pay ask)

        Args:
            legs: List of leg dicts with 'con_id', 'action', and 'ratio'

        Returns:
            Tuple of (combo_bid, combo_ask). Either may be None if any leg bid/ask unavailable.
        """
        combo_bid = 0.0  # What we'd receive if we SELL the combo
        combo_ask = 0.0  # What we'd pay if we BUY the combo

        for leg in legs:
            con_id = leg.get('con_id')
            leg_action = leg.get('action')  # BUY or SELL as defined in the combo
            ratio = leg.get('ratio', 1)

            if not con_id:
                return None, None

            bid, ask = self._get_contract_bid_ask(con_id)
            if bid is None or ask is None:
                return None, None

            if leg_action == "BUY":
                # BUY leg: buy combo = pay ask, sell combo = receive bid
                combo_ask += ask * ratio
                combo_bid += bid * ratio
            else:  # SELL
                # SELL leg: buy combo = receive bid, sell combo = pay ask
                combo_ask -= bid * ratio
                combo_bid -= ask * ratio

        return combo_bid, combo_ask

    async def _collect_orders(self) -> list[dict]:
        """Collect open orders with combo legs grouped together."""
        orders = []
        try:
            # Build position map: conId -> position quantity
            position_map: dict[int, float] = {}
            for pos in self.ib.positions():
                if pos.contract.conId:
                    position_map[pos.contract.conId] = pos.position

            # Request all open orders from all clientIds (not just this connection)
            open_trades = await self.ib.reqAllOpenOrdersAsync()

            # Filter to only include truly open orders (not filled, cancelled, etc.)
            active_statuses = {"Submitted", "PreSubmitted", "PendingSubmit", "ApiPending"}
            for trade in open_trades:
                status = trade.orderStatus.status
                remaining = trade.orderStatus.remaining

                # Skip if status is not active
                if status not in active_statuses:
                    continue

                # Skip if order has no remaining quantity (fully filled)
                if remaining is not None and remaining == 0:
                    continue

                # Use ib_async's isActive() method if available as additional check
                if hasattr(trade, 'isActive') and callable(trade.isActive):
                    if not trade.isActive():
                        continue
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
                    legs_for_bid_ask = []  # For calculating combo bid/ask
                    for leg in contract.comboLegs:
                        # Determine the effective side for this leg
                        # order.action is the overall combo action, leg.action modifies it
                        if order.action == "BUY":
                            leg_side = leg.action  # "BUY" or "SELL"
                        else:
                            # Combo is SELL, so invert the leg action
                            leg_side = "SELL" if leg.action == "BUY" else "BUY"

                        # Look up position for this leg's contract
                        leg_position = position_map.get(leg.conId, 0)
                        leg_action = self._get_order_action(leg_side, leg_position)

                        leg_qty = leg.ratio
                        leg_desc = await self._format_contract_from_conid(leg.conId)

                        legs.append({
                            "action": leg_action,
                            "quantity": leg_qty,
                            "description": leg_desc,
                        })

                        # For bid/ask calculation
                        legs_for_bid_ask.append({
                            "con_id": leg.conId,
                            "action": leg.action,  # Use original leg action for calculation
                            "ratio": leg.ratio,
                        })

                    # Calculate combo bid/ask
                    combo_bid, combo_ask = self._calculate_combo_bid_ask(legs_for_bid_ask)

                    orders.append({
                        "legs": legs,
                        "limit_price": limit_price,
                        "is_credit": is_credit,
                        "status": self._abbreviate_status(trade.orderStatus.status),
                        "is_combo": True,
                        "bid": combo_bid,
                        "ask": combo_ask,
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

                    # Determine action based on current position
                    position_qty = position_map.get(contract.conId, 0)
                    action = self._get_order_action(order.action, position_qty)
                    is_credit = order.action == "SELL"

                    # Get bid/ask for single-leg order
                    bid, ask = self._get_contract_bid_ask(contract.conId)

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
                        "bid": bid,
                        "ask": ask,
                    })
        except Exception as e:
            import traceback
            print(f"[ERROR] _collect_orders exception: {e}")
            traceback.print_exc()

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

                # Convert to local timezone if the time is timezone-aware (IB returns UTC)
                if fill_time.tzinfo is not None:
                    fill_time_local = fill_time.astimezone()
                else:
                    # Assume UTC if no timezone info, convert to local
                    fill_time_local = fill_time.replace(tzinfo=timezone.utc).astimezone()

                time_str = fill_time_local.strftime("%Y-%m-%d %H:%M:%S")
                exec_time_str = fill_time_local.strftime("%b %d %H:%M:%S")

                legs = []
                total_credit = 0.0  # Track net credit/debit
                total_realized_pnl = 0.0  # Sum of realized PnL across all legs
                is_expiration_trade = False  # Track if any leg is an expiration

                for fill in fills:
                    fill_contract = fill.contract
                    execution = fill.execution
                    commission_report = fill.commissionReport

                    # Skip Bag (combo) fills - these are summaries, not real executions
                    # The actual legs are reported as separate fills
                    if isinstance(fill_contract, Bag):
                        continue

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

                    # Get realized PnL from commission report
                    # realizedPNL = 0 means opening trade, != 0 means closing trade
                    realized_pnl = 0.0
                    if commission_report and commission_report.realizedPNL:
                        # IB uses a large number (1.7976931348623157e+308) to indicate "no value"
                        if commission_report.realizedPNL < 1e300:
                            realized_pnl = commission_report.realizedPNL

                    # Check for expiration event: clientId 0 and price 0 indicates
                    # options held into expiration (system-generated, not a real trade)
                    is_expiration = execution.clientId == 0 and execution.price == 0

                    # Determine action based on expiration or realized PnL
                    if is_expiration:
                        action = "EXP"
                        is_expiration_trade = True
                    else:
                        # If realizedPNL != 0, this is a closing trade
                        is_closing = realized_pnl != 0
                        if execution.side == "SLD":
                            action = "STC" if is_closing else "STO"
                        else:
                            action = "BTC" if is_closing else "BTO"

                    # Track credit/debit: sells are credits, buys are debits
                    fill_value = execution.price * qty
                    if execution.side == "SLD":
                        total_credit += fill_value
                    else:
                        total_credit -= fill_value

                    # Accumulate realized PnL for the spread
                    total_realized_pnl += realized_pnl

                    legs.append({
                        "action": action,
                        "quantity": qty,
                        "description": description,
                    })

                # Determine if overall trade was a credit or debit
                is_credit = total_credit > 0
                # For spreads, divide by one leg's quantity (not sum of all legs)
                # This gives the per-spread price rather than averaging across legs
                qty_divisor = legs[0]["quantity"] if legs else 1
                fill_price = abs(total_credit) / max(qty_divisor, 1)

                # PnL: None if opening trade (total = 0), otherwise show the realized PnL
                pnl = total_realized_pnl if total_realized_pnl != 0 else None

                trades.append({
                    "time_sort": time_str,
                    "exec_time": exec_time_str,
                    "legs": legs,
                    "fill_price": fill_price,
                    "is_credit": is_credit,
                    "pnl": pnl,
                    "is_combo": len(legs) > 1,
                    "is_expiration": is_expiration_trade,
                })
        except Exception as e:
            import traceback
            print(f"[ERROR] _collect_trades exception: {e}")
            traceback.print_exc()

        trades.sort(key=lambda x: x["time_sort"], reverse=True)
        return trades

    async def _collect_chart_contracts_and_update_streams(self) -> list[dict]:
        """Collect contracts for charting and manage bar data streams.

        Collects contracts from positions and open orders.
        For options/futures options, also includes the underlying.
        Starts streams for new contracts, stops streams for removed ones.
        """
        chart_contracts: list[dict] = []
        needed_con_ids: set[int] = set()
        contracts_to_stream: dict[int, Contract] = {}  # conId -> Contract

        try:
            # Collect contracts from positions
            for pos in self.ib.positions():
                contract = pos.contract
                if not contract.conId:
                    continue

                self._contract_cache[contract.conId] = contract
                needed_con_ids.add(contract.conId)
                contracts_to_stream[contract.conId] = contract

                # For options, also need the underlying
                if isinstance(contract, (Option, FuturesOption)):
                    underlying = await self._get_underlying_contract(contract)
                    if underlying and underlying.conId:
                        self._contract_cache[underlying.conId] = underlying
                        needed_con_ids.add(underlying.conId)
                        contracts_to_stream[underlying.conId] = underlying

            # Collect contracts from open orders
            for trade in self.ib.openTrades():
                contract = trade.contract
                if not contract.conId:
                    continue

                # Skip Bag contracts - we handle their legs separately
                if isinstance(contract, Bag):
                    # Process combo legs
                    if contract.comboLegs:
                        for leg in contract.comboLegs:
                            leg_contract = self._contract_cache.get(leg.conId)
                            if not leg_contract:
                                # Leg not in cache - qualify it from the conId
                                leg_contract = Contract(conId=leg.conId)
                                qualified = await self.ib.qualifyContractsAsync(leg_contract)
                                if qualified:
                                    leg_contract = qualified[0]
                                    self._contract_cache[leg.conId] = leg_contract
                                else:
                                    continue  # Couldn't qualify, skip this leg

                            needed_con_ids.add(leg.conId)
                            contracts_to_stream[leg.conId] = leg_contract

                            # For options, also need the underlying
                            if isinstance(leg_contract, (Option, FuturesOption)):
                                underlying = await self._get_underlying_contract(leg_contract)
                                if underlying and underlying.conId:
                                    self._contract_cache[underlying.conId] = underlying
                                    needed_con_ids.add(underlying.conId)
                                    contracts_to_stream[underlying.conId] = underlying
                else:
                    self._contract_cache[contract.conId] = contract
                    needed_con_ids.add(contract.conId)
                    contracts_to_stream[contract.conId] = contract

                    # For options, also need the underlying
                    if isinstance(contract, (Option, FuturesOption)):
                        underlying = await self._get_underlying_contract(contract)
                        if underlying and underlying.conId:
                            self._contract_cache[underlying.conId] = underlying
                            needed_con_ids.add(underlying.conId)
                            contracts_to_stream[underlying.conId] = underlying

            # Start streams for new contracts
            for con_id, contract in contracts_to_stream.items():
                if con_id not in self._bar_subscriptions:
                    await self._start_bar_stream(contract)

            # Stop streams for contracts no longer needed
            for con_id in list(self._bar_subscriptions.keys()):
                if con_id not in needed_con_ids:
                    self._stop_bar_stream(con_id)

            # Build chart contracts list for frontend (underlyings first)
            underlyings = []
            options = []
            for con_id, contract in contracts_to_stream.items():
                is_option = isinstance(contract, (Option, FuturesOption))
                if is_option:
                    # Format options same as elsewhere: short date, full underlying, strike, right
                    under_symbol = await self._get_under_symbol(contract)
                    exp_short = self._format_expiration(contract.lastTradeDateOrContractMonth)
                    strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
                    right = self._format_right(contract.right)
                    label = f"{exp_short} {under_symbol} {strike} {right}"
                    options.append({
                        "conId": con_id,
                        "label": label,
                        "isUnderlying": False,
                    })
                else:
                    # Format underlyings: localSymbol longName contractMonth
                    label = await self._format_underlying_label(contract)
                    underlyings.append({
                        "conId": con_id,
                        "label": label,
                        "isUnderlying": True,
                    })

            # Put underlyings first so the frontend picks them by default
            chart_contracts = underlyings + options

        except Exception as e:
            import traceback
            print(f"[Chart] Error collecting chart contracts: {e}")
            traceback.print_exc()

        return chart_contracts

    # -------------------------------------------------------------------------
    # Bar Data Streaming (for charts)
    # -------------------------------------------------------------------------

    async def _start_bar_stream(self, contract: Contract) -> bool:
        """Start a bar data stream for a contract with keepUpToDate.

        This must be called from the main event loop.
        Returns True if stream started successfully, False otherwise.
        """
        con_id = contract.conId
        if con_id in self._bar_subscriptions:
            return True  # Already streaming

        # Qualify the contract first to ensure all fields are populated
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if qualified:
                contract = qualified[0]
            else:
                print(f"[Chart] Could not qualify contract {contract.localSymbol or contract.symbol}")
                return False
        except Exception as e:
            print(f"[Chart] Error qualifying contract {contract.localSymbol or contract.symbol}: {e}")
            return False

        # For options/futures options, use real-time bars (no historical data)
        # For underlyings, use historical data with keepUpToDate
        if isinstance(contract, (Option, FuturesOption)):
            return await self._start_realtime_bar_stream(contract)
        else:
            return await self._start_historical_bar_stream(contract)

    async def _start_historical_bar_stream(self, contract: Contract) -> bool:
        """Start a historical bar stream with keepUpToDate for non-option contracts."""
        con_id = contract.conId
        what_to_show_options = ["TRADES", "MIDPOINT"]

        for what_to_show in what_to_show_options:
            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 min",
                    whatToShow=what_to_show,
                    useRTH=False,
                    formatDate=1,
                    keepUpToDate=True,
                )

                if bars is not None and len(bars) > 0:
                    self._bar_subscriptions[con_id] = bars
                    self._store_bar_data(con_id, bars)
                    bars.updateEvent += lambda b, hasNewBar, cid=con_id: self._on_bar_update(cid, b, hasNewBar)
                    print(f"[Chart] Started historical bar stream for {contract.localSymbol or contract.symbol} ({con_id}) using {what_to_show}")

                    # Also request market data for bid/ask
                    try:
                        ticker = self.ib.reqMktData(contract, snapshot=False, regulatorySnapshot=False)
                        if ticker is not None:
                            self._bid_ask_tickers[con_id] = ticker
                    except Exception as e:
                        print(f"[Chart] Could not get bid/ask ticker for {contract.localSymbol or contract.symbol}: {e}")

                    return True

            except Exception as e:
                print(f"[Chart] {what_to_show} failed for {contract.localSymbol or contract.symbol}: {e}")
                continue

        print(f"[Chart] Could not start bar stream for {contract.localSymbol or contract.symbol}")
        return False

    async def _start_realtime_bar_stream(self, contract: Contract) -> bool:
        """Start a real-time market data stream for options using reqMktData.

        Uses the same pattern as IbkrOptionChain._fetch_option_ticker which works
        reliably for streaming options data. Builds 1-minute OHLC bars from tick updates.
        """
        con_id = contract.conId

        try:
            # Use reqMktData with snapshot=False for streaming (like _fetch_option_ticker)
            ticker = self.ib.reqMktData(contract, snapshot=False, regulatorySnapshot=False)

            if ticker is not None:
                self._bar_subscriptions[con_id] = ticker
                self._bid_ask_tickers[con_id] = ticker  # Also store for bid/ask access
                # Initialize with empty bar data - will build up over time
                with self._bar_data_lock:
                    self._bar_data[con_id] = []
                    self._bar_aggregation[con_id] = {
                        "current_minute": None,
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": 0,
                        "last_cumulative_volume": 0,
                    }

                # Subscribe to ticker updates - will aggregate into 1-minute bars
                ticker.updateEvent += lambda t, cid=con_id: self._on_ticker_update(cid, t)
                print(f"[Chart] Started market data stream for {contract.localSymbol or contract.symbol} ({con_id})")
                return True

        except Exception as e:
            print(f"[Chart] Market data stream failed for {contract.localSymbol or contract.symbol}: {e}")

        print(f"[Chart] Could not start market data stream for {contract.localSymbol or contract.symbol}")
        return False

    def _on_ticker_update(self, con_id: int, ticker) -> None:
        """Callback for ticker updates - aggregate into 1-minute OHLC bars.

        Uses bid/ask midpoint for price, falling back to last price if available.
        This mirrors how IbkrOptionChain streams options data.
        """
        if not ticker:
            return

        # Calculate price from bid/ask midpoint, or use last price as fallback
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        last = ticker.last if ticker.last and ticker.last > 0 else None

        if bid and ask:
            price = (bid + ask) / 2
        elif last:
            price = last
        else:
            return  # No valid price yet

        # Get current time and round down to the minute
        tick_time = ticker.time if ticker.time else datetime.now()
        current_minute = tick_time.replace(second=0, microsecond=0)
        minute_timestamp = int(current_minute.timestamp())

        # Get volume from ticker (if available)
        tick_volume = getattr(ticker, 'volume', 0) or 0

        with self._bar_data_lock:
            if con_id not in self._bar_aggregation:
                self._bar_aggregation[con_id] = {
                    "current_minute": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": None,
                    "volume": 0,
                    "last_cumulative_volume": tick_volume,
                }

            agg = self._bar_aggregation[con_id]

            if agg["current_minute"] != minute_timestamp:
                # New minute started - save the previous bar if we have one
                if agg["current_minute"] is not None and agg["open"] is not None:
                    bar_data = self._bar_data.get(con_id, [])
                    bar_data.append({
                        "time": agg["current_minute"],
                        "open": agg["open"],
                        "high": agg["high"],
                        "low": agg["low"],
                        "close": agg["close"],
                        "volume": agg["volume"],
                    })
                    self._bar_data[con_id] = bar_data

                # Start new minute
                agg["current_minute"] = minute_timestamp
                agg["open"] = price
                agg["high"] = price
                agg["low"] = price
                agg["close"] = price
                agg["volume"] = 0
                agg["last_cumulative_volume"] = tick_volume
            else:
                # Same minute - update OHLC
                agg["high"] = max(agg["high"], price) if agg["high"] else price
                agg["low"] = min(agg["low"], price) if agg["low"] else price
                agg["close"] = price
                # Track volume delta (ticker.volume is cumulative for the day)
                if tick_volume > agg.get("last_cumulative_volume", 0):
                    agg["volume"] += tick_volume - agg["last_cumulative_volume"]
                    agg["last_cumulative_volume"] = tick_volume

    def _store_bar_data(self, con_id: int, bars) -> None:
        """Convert BarDataList to our format and store thread-safely."""
        bar_list = [
            {
                "time": int(bar.date.timestamp()),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": getattr(bar, 'volume', 0) or 0,
            }
            for bar in bars
        ]
        with self._bar_data_lock:
            self._bar_data[con_id] = bar_list

    def _on_bar_update(self, con_id: int, bars, _hasNewBar: bool) -> None:
        """Callback when bar data updates from IB."""
        self._store_bar_data(con_id, bars)

    def _stop_bar_stream(self, con_id: int) -> None:
        """Stop a bar data stream (handles both historical and market data streams)."""
        if con_id in self._bar_subscriptions:
            subscription = self._bar_subscriptions[con_id]
            try:
                # Try different cancel methods depending on subscription type
                # Historical data uses cancelHistoricalData
                # Market data (Ticker) uses cancelMktData
                try:
                    self.ib.cancelHistoricalData(subscription)
                except Exception:
                    try:
                        self.ib.cancelMktData(subscription.contract)
                    except Exception:
                        pass  # Already cancelled or invalid
            except Exception as e:
                print(f"[Chart] Error stopping bar stream for {con_id}: {e}")
            del self._bar_subscriptions[con_id]

            with self._bar_data_lock:
                if con_id in self._bar_data:
                    del self._bar_data[con_id]
                if con_id in self._bar_aggregation:
                    del self._bar_aggregation[con_id]

        # Also clean up bid/ask ticker if separate from bar subscription
        if con_id in self._bid_ask_tickers:
            ticker = self._bid_ask_tickers[con_id]
            # Only cancel if it wasn't the same as bar subscription (already cancelled above)
            if con_id not in self._bar_subscriptions or self._bar_subscriptions.get(con_id) != ticker:
                try:
                    self.ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
            del self._bid_ask_tickers[con_id]

    def _get_bar_data(self, con_id: int) -> list[dict]:
        """Get bar data for a contract (thread-safe, called from Flask).

        Includes the current incomplete bar from aggregation state for options.
        Timestamps are shifted from UTC to local timezone (accounting for DST).
        """
        with self._bar_data_lock:
            bars = list(self._bar_data.get(con_id, []))

            # Include current incomplete bar from aggregation (for options streaming)
            agg = self._bar_aggregation.get(con_id)
            if agg and agg["current_minute"] is not None and agg["open"] is not None:
                bars.append({
                    "time": agg["current_minute"],
                    "open": agg["open"],
                    "high": agg["high"],
                    "low": agg["low"],
                    "close": agg["close"],
                    "volume": agg.get("volume", 0),
                })

            # Shift timestamps from UTC to local timezone
            # LightweightCharts doesn't have timezone settings, so we shift the data
            return [self._shift_bar_to_local_tz(bar) for bar in bars]

    def _shift_bar_to_local_tz(self, bar: dict) -> dict:
        """Shift a bar's timestamp from UTC to local timezone (accounting for DST)."""
        utc_ts = bar["time"]
        # Convert UTC timestamp to datetime, get local timezone offset at that time
        utc_dt = datetime.utcfromtimestamp(utc_ts)
        local_dt = datetime.fromtimestamp(utc_ts)
        # The offset is the difference between local and UTC
        offset_seconds = int((local_dt - utc_dt).total_seconds())
        return {
            "time": utc_ts + offset_seconds,
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar.get("volume", 0),
        }

    # -------------------------------------------------------------------------
    # Historical Data (legacy - uses thread-safe async call)
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

    async def _data_collection_loop(self) -> None:
        """Collect data periodically in the background.

        This runs as an async task in the main event loop, collecting
        positions, orders, trades, and chart data every 2 seconds.
        """
        while self._running:
            try:
                if self.ib is not None and self.ib.isConnected():
                    await self.collect_data()
            except Exception as e:
                print(f"[Webapp] Error in data collection loop: {e}")
            await asyncio.sleep(2)

    def start(
        self,
        host: str,
        port: int,
        client_id: int,
        webapp_port: int = 5000,
        day_start_time: time | None = None,
        day_stop_time: time | None = None,
    ) -> None:
        """Start the webapp with its own IB connection.

        This method connects to IB, starts the data collection loop,
        and launches the Flask server in a daemon thread.

        Args:
            host: IB Gateway/TWS host address
            port: IB Gateway/TWS port
            client_id: Client ID for the IB connection (must be unique)
            webapp_port: Port to run the Flask server on
            day_start_time: When live mode starts (displays "Sleeping" outside this window)
            day_stop_time: When live mode ends (displays "Sleeping" outside this window)
        """
        self._day_start_time = day_start_time
        self._day_stop_time = day_stop_time
        import nest_asyncio
        nest_asyncio.apply()

        # Create and connect the IB instance
        self.ib = IB()
        self.ib.connect(
            host=host,
            port=port,
            clientId=client_id,
        )
        print(f"[Webapp] Connected to IB at {host}:{port} with client ID {client_id}")

        # Capture the current event loop for async calls from Flask thread
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        self._running = True

        # Start the data collection loop as an async task
        self._data_collection_task = asyncio.ensure_future(self._data_collection_loop())

        def run_flask():
            # Suppress Flask's default logging for cleaner output
            import logging
            log = logging.getLogger('werkzeug')
            log.setLevel(logging.ERROR)

            self._app.run(
                host="0.0.0.0",
                port=webapp_port,
                threaded=True,
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=run_flask, daemon=True)
        self._thread.start()
        print(f"Webapp started on http://localhost:{webapp_port}")

    def stop(self) -> None:
        """Stop the webapp and clean up resources."""
        self._running = False

        # Cancel data collection task
        if self._data_collection_task is not None:
            self._data_collection_task.cancel()
            self._data_collection_task = None

        # Stop all bar data streams
        for con_id in list(self._bar_subscriptions.keys()):
            self._stop_bar_stream(con_id)

        # Disconnect from IB
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
            print("[Webapp] Disconnected from IB")
