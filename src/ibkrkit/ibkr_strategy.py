import asyncio
import nest_asyncio

from datetime import datetime, timedelta
from traceback import print_exception
from typing import List, Union

from ib_async import *


class IbkrStrategy:
    name: str
    version: str    
    
    def __init__(self):
        self.now: datetime = None

    def run(self, host: str = '127.0.0.1', port: int = 7496, client_id: int = 1, account_id: str = ""):
        nest_asyncio.apply()
        asyncio.run(self._run(host, port, client_id, account_id=account_id))
        

    async def _run(self, host: str, port: int, client_id: int, account_id: str):        
        self.ib = IB()
        self.ib.connect(host=host, port=port, clientId=client_id, account=account_id)  
    
        start_time = datetime.now()
        end_time = datetime.now() + timedelta(days=1)
    
        # TODO: We may want to change the tick frequency to be a parameter
        time_range = util.timeRangeAsync(start_time, end_time, 5)
        self.now = await anext(time_range)
        try:
            await self.on_start()
            async for now in time_range:
                await self._tick(now)
        except Exception as e:
            print_exception(e)
        finally:
            await self.on_stop()
            self.ib.disconnect()
        
        
    async def on_start(self):
        raise NotImplementedError("on_start method must be implemented by subclass.")
        
        
    async def _tick(self, now: datetime):
        self.now = now
        await self.tick()
        
        
    async def tick(self):
        raise NotImplementedError("tick method must be implemented by subclass.")
        
        
    async def on_stop(self):
        raise NotImplementedError("on_stop method must be implemented by subclass.")
    
    
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
    
    
    def print_msg(self, msg: str) -> None:
        print(f"{self.now.strftime('%Y-%m-%d %H:%M:%S')} | {msg}") 
    
    
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
