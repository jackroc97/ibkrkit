import asyncio
from datetime import datetime

import pandas as pd
from ib_async import *


class IbkrDataStore:

    @classmethod
    async def connect(cls, host: str = '127.0.0.1', port: int = 7497, client_id: int = 1) -> None:
        cls._ib = IB()
        await cls._ib.connect(host, port, clientId=client_id)
        cls._market_data: dict[int, Ticker] = {}
        cls._lock = asyncio.Lock()
        

    @classmethod
    async def req_market_data_stream(cls, contract: Contract) -> Ticker:
        qualified_contract = (await cls._ib.qualifyContractsAsync(contract))[0]
        key = qualified_contract.conId
        async with cls._lock:
            if key not in cls._market_data:
                ticker = cls._ib.reqMktData(qualified_contract, snapshot=False, regulatorySnapshot=False)
                cls._market_data[key] = ticker
            return cls._market_data[key]
                

    @classmethod
    async def req_market_data_snapshot(cls, contract: Contract) -> Ticker:
        qualified_contract = (await cls._ib.qualifyContractsAsync(contract))[0]
        return cls._ib.reqMktData(qualified_contract, snapshot=True, regulatorySnapshot=False)


    @classmethod
    def req_sec_def_opt_params(cls, symbol: str, exchange: str, sec_type: str, con_id: int) -> list:
        return cls._ib.reqSecDefOptParams(symbol, exchange, sec_type, con_id)


    @classmethod
    async def disconnect(cls) -> None:
        await cls._ib.disconnect()
