import asyncio

from ib_async import *
from typing import List, Optional, Union

from .ibkr_strategy import IbkrStrategy


async def wait_for_ibkr_ready(host="127.0.0.1", port=4004, client_id: int = 99, retries=30, delay=5):
    """
    Try connecting to the IBKR Gateway every `delay` seconds until successful
    or until `retries` are exhausted.
    """
    ib = IB()
    for attempt in range(1, retries + 1):
        try:
            print(f"Attempt {attempt}: Connecting to IBKR at {host}:{port}...")
            await ib.connectAsync(host, port, clientId=client_id)
            if ib.isConnected():
                print("IBKR connection established and ready.")
                ib.disconnect()
                return True
        except Exception as e:
            print(f"Connection failed ({e}). Retrying in {delay}s...")
        await asyncio.sleep(delay)

    print("Failed to connect to IBKR after multiple attempts.")
    return False


def place_bracket_order(strategy: IbkrStrategy, contract: Contract, open_action: str, quantity: int, limit_price: float, take_profit_price: float, stop_loss_price: float = None):
    ...
    close_action = "SELL" if open_action == "BUY" else "BUY"
    
    order = LimitOrder(action=open_action, totalQuantity=quantity, lmtPrice=limit_price, tif="GTC")
    take_profit = LimitOrder(action=close_action, totalQuantity=quantity, lmtPrice=take_profit_price, tif="GTC")
            
    order.ocaGroup = f"oco_{strategy.now.strftime('%Y%m%d_%H%M%S')}"
    take_profit.ocaGroup = order.ocaGroup
            
    order.orderId = strategy.ib.client.getReqId()
    order.transmit = False
    take_profit.orderId = strategy.ib.client.getReqId()
    take_profit.parentId = order.orderId
    take_profit.transmit = True
    
    orders = [order, take_profit]
    
    if stop_loss_price is not None:
        stop_loss = StopOrder(action=close_action, totalQuantity=quantity, stopPrice=stop_loss_price, tif="GTC")
        stop_loss.ocaGroup = order.ocaGroup

        stop_loss.orderId = strategy.ib.client.getReqId()
        stop_loss.parentId = order.orderId
        
        # Set the take-profit order transmit property to False if we have a stop loss
        orders[-1].transmit = False
        stop_loss.transmit = True
        
        orders.append(stop_loss)
            
    trades = []
    for ord in orders:
        trade = strategy.ib.placeOrder(contract, ord)
        trade.filledEvent += strategy.on_order_filled
        trade.fillEvent += strategy.on_order_partial_fill
        trade.statusEvent += strategy.on_order_status
        trade.modifyEvent += strategy.on_order_modified
        trade.cancelEvent += strategy.on_order_cancelled
        trades.append(trade)
    return trades
    

def place_spread_order(strategy: IbkrStrategy, short_contract: Contract, long_contract: Contract, open_action: str, quantity: int, limit_price: float, take_profit_price: float = None, stop_loss_price: float = None, leg_out: bool = False):
    close_action = "SELL" if open_action == "BUY" else "BUY"
    oca_group = f"oco_{strategy.now.strftime('%Y%m%d_%H%M%S')}"

    # Build the Bag combo contract
    bag = Bag()
    bag.symbol = short_contract.symbol
    bag.exchange = "SMART"
    bag.currency = short_contract.currency

    short_leg = ComboLeg()
    short_leg.conId = short_contract.conId
    short_leg.ratio = 1
    short_leg.action = "SELL"
    short_leg.exchange = short_contract.exchange or "SMART"

    long_leg = ComboLeg()
    long_leg.conId = long_contract.conId
    long_leg.ratio = 1
    long_leg.action = "BUY"
    long_leg.exchange = long_contract.exchange or "SMART"

    bag.comboLegs = [short_leg, long_leg]

    # Opening limit order
    order = LimitOrder(action=open_action, totalQuantity=quantity, lmtPrice=limit_price, tif="GTC")
    order.ocaGroup = oca_group
    order.orderId = strategy.ib.client.getReqId()
    order.transmit = False

    # List of (contract, order) pairs to submit
    orders = [(bag, order)]

    if leg_out:
        # Closing orders target the short leg only; a conditional order closes the long leg
        oca_long_group = f"oco_long_{strategy.now.strftime('%Y%m%d_%H%M%S')}"

        if take_profit_price is not None:
            tp_order = LimitOrder(action="BUY", totalQuantity=quantity, lmtPrice=take_profit_price, tif="GTC")
            tp_order.ocaGroup = oca_group
            tp_order.orderId = strategy.ib.client.getReqId()
            tp_order.parentId = order.orderId
            tp_order.transmit = False
            orders.append((short_contract, tp_order))

            long_tp_condition = ExecutionCondition()
            long_tp_condition.secType = short_contract.secType
            long_tp_condition.exch = short_contract.exchange or "SMART"
            long_tp_condition.symbol = short_contract.symbol

            long_tp_order = Order()
            long_tp_order.action = "SELL"
            long_tp_order.totalQuantity = quantity
            long_tp_order.orderType = "MKT"
            long_tp_order.tif = "GTC"
            long_tp_order.conditions = [long_tp_condition]
            long_tp_order.conditionsIgnoreRth = True
            long_tp_order.ocaGroup = oca_long_group
            long_tp_order.orderId = strategy.ib.client.getReqId()
            long_tp_order.parentId = order.orderId
            long_tp_order.transmit = False
            orders.append((long_contract, long_tp_order))

        if stop_loss_price is not None:
            sl_order = StopOrder(action="BUY", totalQuantity=quantity, stopPrice=stop_loss_price, tif="GTC")
            sl_order.ocaGroup = oca_group
            sl_order.orderId = strategy.ib.client.getReqId()
            sl_order.parentId = order.orderId
            sl_order.transmit = False
            orders.append((short_contract, sl_order))

            long_sl_condition = ExecutionCondition()
            long_sl_condition.secType = short_contract.secType
            long_sl_condition.exch = short_contract.exchange or "SMART"
            long_sl_condition.symbol = short_contract.symbol

            long_sl_order = Order()
            long_sl_order.action = "SELL"
            long_sl_order.totalQuantity = quantity
            long_sl_order.orderType = "MKT"
            long_sl_order.tif = "GTC"
            long_sl_order.conditions = [long_sl_condition]
            long_sl_order.conditionsIgnoreRth = True
            long_sl_order.ocaGroup = oca_long_group
            long_sl_order.orderId = strategy.ib.client.getReqId()
            long_sl_order.parentId = order.orderId
            long_sl_order.transmit = False
            orders.append((long_contract, long_sl_order))

    else:
        if take_profit_price is not None:
            tp_order = LimitOrder(action=close_action, totalQuantity=quantity, lmtPrice=take_profit_price, tif="GTC")
            tp_order.ocaGroup = oca_group
            tp_order.orderId = strategy.ib.client.getReqId()
            tp_order.parentId = order.orderId
            tp_order.transmit = False
            orders.append((bag, tp_order))

        if stop_loss_price is not None:
            sl_order = StopOrder(action=close_action, totalQuantity=quantity, stopPrice=stop_loss_price, tif="GTC")
            sl_order.ocaGroup = oca_group
            sl_order.orderId = strategy.ib.client.getReqId()
            sl_order.parentId = order.orderId
            sl_order.transmit = False
            orders.append((bag, sl_order))

    # Last order in the chain triggers submission of all held orders
    orders[-1][1].transmit = True

    trades = []
    for contract, ord in orders:
        trade = strategy.ib.placeOrder(contract, ord)
        trade.filledEvent += strategy.on_order_filled
        trade.fillEvent += strategy.on_order_partial_fill
        trade.statusEvent += strategy.on_order_status
        trade.modifyEvent += strategy.on_order_modified
        trade.cancelEvent += strategy.on_order_cancelled
        trades.append(trade)

    return trades
    

def position_as_str(position: Union[Position, List[Position]]) -> str:
        """
        1.0 ES 20251003P6680 (123456789) @ 1.5
        """
        if isinstance(position, list):
            return "[" + ', '.join([position_as_str(p) for p in position]) + "]"
        return f"{position.position} {contract_as_str(position.contract)} @ {position.avgCost if position.avgCost is not None else 'N/A'}"
    
    
def contract_as_str(contract: Union[Contract, List[Contract]]) -> str:
    """
    ES 20251003P6680 (123456789)
    """
    if isinstance(contract, list):
        return "[" + ', '.join([contract_as_str(c) for c in contract]) + "]"

    if isinstance(contract, Bag):
        legs_str = ', '.join([f"{leg.ratio}x {leg.conId}" for leg in contract.comboLegs])
        return f"{{{legs_str}}}"
    elif isinstance(contract, FuturesOption) or isinstance(contract, Option):
        return f"{contract.symbol} {contract.lastTradeDateOrContractMonth}{contract.right}{int(contract.strike)} ({contract.conId if contract.conId is not None else ''})".strip()
    else:
        return f"{contract.localSymbol} ({contract.conId if contract.conId is not None else ''})".strip()


def trade_as_str(trade: Union[Trade, List[Trade]]) -> str:
    """
    BUY 1.0 {-1x ES 20251003P6680, 1x ES 20251003P6630} @ LMT 1.50 (PendingSubmit)
    """
    if isinstance(trade, list):
        return "[" + ', '.join([trade_as_str(t) for t in trade]) + "]"

    order_type = trade.order.orderType if trade.order.orderType is not None else "MKT"
    order_price = trade.order.lmtPrice if trade.order.lmtPrice is not None else trade.order.auxPrice if trade.order.auxPrice is not None else ""
    return f"{trade.order.action} {trade.order.totalQuantity} {contract_as_str(trade.contract)} @ {order_type} {order_price} ({trade.orderStatus.status})"


def fill_as_str(fill: Union[Fill, List[Fill]]) -> str:
    """
    1.0 ES 20251003P6680 (123456789) @ 1.5 (Exec ID: 0001)
    """
    if isinstance(fill, list):
        return "[" + ', '.join([fill_as_str(f) for f in fill]) + "]"
    return f"{fill.execution.shares} {contract_as_str(fill.contract)} @ {fill.execution.price} (Exec ID: {fill.execution.execId})"