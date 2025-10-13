from datetime import datetime

import pandas as pd
from ib_async import *


class IbkrDataStream:
    
    def __init__(self, ib: IB, contract: Contract, on_update: callable = None) -> None:
        self.contract = contract
        self.on_update = on_update

        self._ib = ib
        self._df = pd.DataFrame()
        
        
    @classmethod
    async def create(cls, ib: IB, contract: Contract, on_update: callable = None) -> 'IbkrDataStream':
        stream = cls(ib, contract, on_update)
        await stream._start()
        return stream
        
    
    async def _start(self) -> None:
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Starting data stream for {self.contract.localSymbol}...")
        
        # Qualify the contract, request market data, and the default update handler
        await self._ib.qualifyContractsAsync(self.contract)
        self._ticker = self._ib.reqMktData(self.contract, snapshot=False, regulatorySnapshot=False)
        self._ticker.updateEvent += self._on_update

        # Add a custom update handler if provided
        if self.on_update is not None:
            self._ticker.updateEvent += self.on_update
        
        # TODO: May want to parameterize the timeout length
        t = datetime.now()
        while not len(self._df):
            util.sleep(1)
            if (datetime.now() - t).seconds > 30:
                raise TimeoutError("Timeout waiting for initial market data.")
        
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Data stream for {self.contract.localSymbol} started.")    
                                
    
    def get(self, name: str, at_time: datetime = None, bars_ago: int = None) -> any:
        if at_time is not None:
            if at_time in self._df.index:
                return self._df.at[at_time, name]
            else:
                raise ValueError(f"No data found for {name} at {at_time}.")
        
        elif bars_ago is not None:
            index = self._df.index[-bars_ago - 1]
            if index in self._df.index:
                return self._df.at[index, name]
            else:
                raise ValueError(f"No data found for {name} {bars_ago} bars ago.")
        # Default and most common case
        # Return the last value of the requested column
        else:
            return self._df[name].iloc[-1] if not self._df.empty else None
    
    
    def _on_update(self, ticker: Ticker) -> None:
        df = pd.DataFrame([{
            "date": ticker.time,
            "bid": ticker.bid,
            "ask": ticker.ask,
            "last": ticker.last,
            "volume": ticker.volume,
            "open": ticker.open,
            "high": ticker.high,
            "low": ticker.low,
            "close": ticker.close,
            "vwap": ticker.vwap,
        }])
        
        # Dates from ibkr don't appear to account for DST
        # This massages the date data to account for DST and then 
        # converts the DataFrame index to a DatetimeIndex
        # TODO: May want to customize which timezone we convert to
        df.index = pd.DatetimeIndex(
            df["date"].dt.tz_convert("US/Eastern"))
            
        # Calculate the UTC offset for each timestamp
        utc_offsets = df.index.map(lambda ts: ts.utcoffset())

        # Convert the index to UTC and remove the timezone
        df.index = df.index.tz_convert('UTC').tz_localize(None)

        # Add the previously calculated UTC offset back to the index
        df.index = df.index + utc_offsets

        # Drop the old date field
        df.drop('date', axis=1, inplace=True)
    
        # Combine with existing data
        if not self._df.empty:
            df = pd.concat([self._df, df])
            df = df[~df.index.duplicated(keep='last')]
            df = df.sort_index()
    
        # Do any user-specified processing here
        if self.on_update:
           df = self.on_update(df)
    
        self._df = df        
        self.is_first_update = False
