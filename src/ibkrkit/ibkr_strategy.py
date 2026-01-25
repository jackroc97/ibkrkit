import asyncio
import nest_asyncio
import warnings

from datetime import datetime, time
from traceback import print_exception
from typing import List, Optional, Union

from ib_async import *


class IbkrStrategy:
    name: str
    version: str

    def __init__(self):
        self.now: datetime = None
        self._is_live: bool = False
        self._day_start_time: Optional[time] = None
        self._day_stop_time: Optional[time] = None
        self._tick_freq_seconds: int = 5
        self._weekday_only: bool = True

    @property
    def is_live(self) -> bool:
        """Returns True if the strategy is currently in live mode."""
        return self._is_live

    @property
    def tick_freq_seconds(self) -> int:
        """Returns the configured tick frequency in seconds."""
        return self._tick_freq_seconds

    @property
    def weekday_only(self) -> bool:
        """Returns True if the strategy only enters live mode on weekdays."""
        return self._weekday_only

    def run(
        self,
        host: str = '127.0.0.1',
        port: int = 7496,
        client_id: int = 1,
        account_id: str = "",
        day_start_time: Optional[time] = None,
        day_stop_time: Optional[time] = None,
        tick_freq_seconds: int = 5,
        weekday_only: bool = True
    ):
        self._day_start_time = day_start_time
        self._day_stop_time = day_stop_time
        self._tick_freq_seconds = tick_freq_seconds
        self._weekday_only = weekday_only
        nest_asyncio.apply()
        asyncio.run(self._run(host, port, client_id, account_id=account_id))


    async def _run(self, host: str, port: int, client_id: int, account_id: str):
        self.ib = IB()
        self.ib.connect(host=host, port=port, clientId=client_id, account=account_id)

        self.now = datetime.now()

        try:
            await self._call_strategy_init()

            # Determine initial mode
            self._is_live = self._should_be_live(self.now)
            if self._is_live:
                await self._call_day_start()

            # Main loop - runs indefinitely until interrupted
            while True:
                self.now = datetime.now()

                # Check for mode transitions
                await self._check_mode_transition()

                # Only call tick when in live mode
                if self._is_live:
                    await self.tick()
                    await asyncio.sleep(self._tick_freq_seconds)
                else:
                    print(f"{self.now.strftime('%Y-%m-%d %H:%M:%S')} | Strategy is in sleep mode between {self._day_stop_time} and {self._day_start_time}")
                    await asyncio.sleep(60) # Sleep for 1 minute in sleep mode
                

        except Exception as e:
            print_exception(e)
        finally:
            # End the day if we're currently live
            if self._is_live:
                await self._call_day_end()
            await self._call_strategy_shutdown()
            self.ib.disconnect()

    def _should_be_live(self, now: datetime) -> bool:
        """Determine if the strategy should be in live mode based on current time."""
        # Never enter live mode on weekends if weekday_only is True
        # weekday() returns 0-4 for Mon-Fri, 5-6 for Sat-Sun
        if self._weekday_only and now.weekday() >= 5:
            return False

        # If no times configured, always live (backward compatible behavior)
        if self._day_start_time is None and self._day_stop_time is None:
            return True

        current_time = now.time()

        # Handle case where only one time is specified
        if self._day_start_time is None:
            return current_time < self._day_stop_time
        if self._day_stop_time is None:
            return current_time >= self._day_start_time

        # Both times specified
        if self._day_start_time <= self._day_stop_time:
            # Normal case: start before stop (e.g., 9:30 to 16:00)
            return self._day_start_time <= current_time < self._day_stop_time
        else:
            # Overnight case: start after stop (e.g., 18:00 to 06:00)
            return current_time >= self._day_start_time or current_time < self._day_stop_time

    async def _check_mode_transition(self) -> None:
        """Check for mode changes and call appropriate callbacks."""
        should_be_live = self._should_be_live(self.now)

        if should_be_live and not self._is_live:
            # Transitioning to live mode
            self._is_live = True
            await self._call_day_start()
        elif not should_be_live and self._is_live:
            # Transitioning to sleep mode
            self._is_live = False
            await self._call_day_end()

    async def _call_strategy_init(self) -> None:
        """Call strategy init, with backward compatibility for on_start."""
        # Check if subclass overrides on_start (backward compatibility)
        if type(self).on_start is not IbkrStrategy.on_start:
            warnings.warn(
                "on_start() is deprecated, use on_strategy_init() instead",
                DeprecationWarning,
                stacklevel=2
            )
            await self.on_start()
        else:
            await self.on_strategy_init()

    async def _call_strategy_shutdown(self) -> None:
        """Call strategy shutdown, with backward compatibility for on_stop."""
        # Check if subclass overrides on_stop (backward compatibility)
        if type(self).on_stop is not IbkrStrategy.on_stop:
            warnings.warn(
                "on_stop() is deprecated, use on_strategy_shutdown() instead",
                DeprecationWarning,
                stacklevel=2
            )
            await self.on_stop()
        else:
            await self.on_strategy_shutdown()

    async def _call_day_start(self) -> None:
        """Call day start callback."""
        await self.on_day_start()

    async def _call_day_end(self) -> None:
        """Call day end callback."""
        await self.on_day_end()

    async def on_strategy_init(self):
        """Called once when the strategy is initialized. Override in subclass."""
        pass

    async def on_strategy_shutdown(self):
        """Called once when the strategy is shutting down. Override in subclass."""
        pass

    async def on_day_start(self):
        """Called each time the strategy enters live mode. Override in subclass."""
        pass

    async def on_day_end(self):
        """Called each time the strategy enters sleep mode. Override in subclass."""
        pass

    # Deprecated methods for backward compatibility
    async def on_start(self):
        """Deprecated: Use on_strategy_init() instead."""
        pass


    async def _tick(self, now: datetime):
        self.now = now
        await self.tick()


    async def tick(self):
        raise NotImplementedError("tick method must be implemented by subclass.")

    # Deprecated method for backward compatibility
    async def on_stop(self):
        """Deprecated: Use on_strategy_shutdown() instead."""
        pass
    
    
    def on_order_filled(self, trade: Trade) -> None:
        pass
    
        
    def on_order_partial_fill(self, trade: Trade, fill: Fill) -> None:
        pass

    
    def on_order_status(self, trade: Trade) -> None:
        pass
    
        
    def on_order_modified(self, trade: Trade) -> None:
        pass


    def on_order_cancelled(self, tratrade: Trade) -> None:
        pass


    def place_bracket_order(self, contract: Contract, open_action: str, quantity: int, limit_price: float, take_profit_price: float, stop_loss_price: float = None) -> list[Trade]:
        close_action = "SELL" if open_action == "BUY" else "BUY"
        
        order = LimitOrder(action=open_action, totalQuantity=quantity, lmtPrice=limit_price, tif="GTC")
        take_profit = LimitOrder(action=close_action, totalQuantity=quantity, lmtPrice=take_profit_price, tif="GTC")
                
        order.ocaGroup = f"oco_{self.now.strftime('%Y%m%d_%H%M%S')}"
        take_profit.ocaGroup = order.ocaGroup
                
        order.orderId = self.ib.client.getReqId()
        order.transmit = False
        take_profit.orderId = self.ib.client.getReqId()
        take_profit.parentId = order.orderId
        take_profit.transmit = True
        
        orders = [order, take_profit]
        
        if stop_loss_price is not None:
            stop_loss = StopOrder(action=close_action, totalQuantity=quantity, stopPrice=stop_loss_price, tif="GTC")
            stop_loss.ocaGroup = order.ocaGroup

            stop_loss.orderId = self.ib.client.getReqId()
            stop_loss.parentId = order.orderId
            
            # Set the take-profit order transmit property to False if we have a stop loss
            orders[-1].transmit = False
            stop_loss.transmit = True
            
            orders.append(stop_loss)
                
        trades = []
        for ord in orders:
            trade = self.ib.placeOrder(contract, ord)
            trade.filledEvent += self.on_order_filled
            trade.fillEvent += self.on_order_partial_fill
            trade.statusEvent += self.on_order_status
            trade.modifyEvent += self.on_order_modified
            trade.cancelEvent += self.on_order_cancelled
            trades.append(trade)
        return trades
    
    
    def print_msg(self, msg: str, overwrite: bool = False) -> None:
        formatted = f"{self.now.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
        if overwrite:
            # Use carriage return to move to start, then ANSI escape to clear line
            print(f"\r\033[K{formatted}", end="", flush=True)
        else:
            print(formatted) 
    
    
    def position_as_str(self, position: Union[Position, List[Position]]) -> str:
        """
        1.0 ES 20251003P6680 (123456789) @ 1.5
        """
        if isinstance(position, list):
            return "[" + ', '.join([self.position_as_str(p) for p in position]) + "]"
        return f"{position.position} {self.contract_as_str(position.contract)} @ {position.avgCost if position.avgCost is not None else 'N/A'}"
    
    
    def contract_as_str(self, contract: Union[Contract, List[Contract]]) -> str:
        """
        ES 20251003P6680 (123456789)
        """
        if isinstance(contract, list):
            return "[" + ', '.join([self.contract_as_str(c) for c in contract]) + "]"
    
        if isinstance(contract, Bag):
            legs_str = ', '.join([f"{leg.ratio}x {leg.conId}" for leg in contract.comboLegs])
            return f"{{{legs_str}}}"
        elif isinstance(contract, FuturesOption) or isinstance(contract, Option):
            return f"{contract.symbol} {contract.lastTradeDateOrContractMonth}{contract.right}{int(contract.strike)} ({contract.conId if contract.conId is not None else ''})".strip()
        else:
            return f"{contract.localSymbol} ({contract.conId if contract.conId is not None else ''})".strip()
    
    
    def trade_as_str(self, trade: Union[Trade, List[Trade]]) -> str:
        """
        BUY 1.0 {-1x ES 20251003P6680, 1x ES 20251003P6630} @ LMT 1.50 (PendingSubmit)
        """
        if isinstance(trade, list):
            return "[" + ', '.join([self.trade_as_str(t) for t in trade]) + "]"
    
        order_type = trade.order.orderType if trade.order.orderType is not None else "MKT"
        order_price = trade.order.lmtPrice if trade.order.lmtPrice is not None else trade.order.auxPrice if trade.order.auxPrice is not None else ""
        return f"{trade.order.action} {trade.order.totalQuantity} {self.contract_as_str(trade.contract)} @ {order_type} {order_price} ({trade.orderStatus.status})"

    
    def fill_as_str(self, fill: Union[Fill, List[Fill]]) -> str:
        """
        1.0 ES 20251003P6680 (123456789) @ 1.5 (Exec ID: 0001)
        """
        if isinstance(fill, list):
            return "[" + ', '.join([self.fill_as_str(f) for f in fill]) + "]"
        return f"{fill.execution.shares} {self.contract_as_str(fill.contract)} @ {fill.execution.price} (Exec ID: {fill.execution.execId})"
