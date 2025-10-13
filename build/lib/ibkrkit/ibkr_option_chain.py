import asyncio

from datetime import date, datetime
from itertools import product

import pandas as pd
from ib_async import *
    

class IbkrOptionChain:
    
    def __init__(self, ib: IB, contract: Contract, max_strike_dist: float, max_dte: int, update_chain_interval: int, excluded_trading_classes: list[str]):
        self.contract = contract
        self.max_strike_dist = max_strike_dist
        self.max_dte = max_dte
        self.update_chain_interval = update_chain_interval
        self.excluded_trading_classes = excluded_trading_classes
        self.started = False
        
        self._ib = ib
        self._chain_flat = pd.DataFrame()
        self._contract_tickers: dict[tuple, Ticker] = {}
        
        
    @classmethod
    async def create(cls, ib: IB, contract: Contract, max_strike_dist: float = 100.0, max_dte: int = 10, update_chain_interval: int = 60, excluded_trading_classes: list[str] = []) -> 'IbkrOptionChain':
        chain = cls(ib, contract, max_strike_dist, max_dte, update_chain_interval, excluded_trading_classes)
        await chain._start()
        return chain
    
        
    async def _start(self) -> None:
        # Perform the initial update of the chain (will get all desired options)
        await self._update_chain(write_status=True)
        
        # Schedule the chain to be continually updated as the underlying price changes
        async def _update_loop():
            while True:
                if self.started:
                    await self._update_chain()
                    await asyncio.sleep(self.update_chain_interval)
        asyncio.create_task(_update_loop())
        self.started = True
        

    async def _update_chain(self, write_status: bool = False) -> None:
        if write_status:
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Fetching options chain for {self.contract.symbol}...", flush=True)
        
        # Request the options chain from IBKR TWS 
        chain = self._ib.reqSecDefOptParams(self.contract.symbol, 
                                            self.contract.exchange, 
                                            self.contract.secType, 
                                            self.contract.conId)
        
        # Flatten the options chain data into a DataFrame
        rows = []
        for _, row in util.df(chain).iterrows():
            expirations = row['expirations']
            strikes = row['strikes']
            for exp, strike in product(expirations, strikes):
                new_row = row.drop(labels=['expirations', 'strikes']).to_dict()
                new_row['expiration'] = exp
                new_row['strike'] = strike
                rows.append(new_row)
        chain_flat = pd.DataFrame(rows)
        
        # Get last price of underlying in order to filter the options by strike distance
        underlying_ticker = self._ib.reqMktData(self.contract, snapshot=True, regulatorySnapshot=False)
        underlying_last = underlying_ticker.last
        
        # Filter the options chain based on max strike distance
        min_strike = underlying_last - self.max_strike_dist
        max_strike = underlying_last + self.max_strike_dist
        chain_flat = chain_flat[(chain_flat['strike'] >= min_strike) & (chain_flat['strike'] <= max_strike)]
        
        # Filter the options chain based on max DTE
        # TODO: Should we assume if its past 1600 EST that we want date > today?
        chain_flat["expiration_dt"] = pd.to_datetime(chain_flat["expiration"], format="%Y%m%d")
        chain_flat = chain_flat[chain_flat["expiration_dt"] <= (datetime.now() + pd.Timedelta(days=self.max_dte))]
        chain_flat["expire_unix"] = chain_flat["expiration_dt"].apply(lambda x: int(x.timestamp()))
        
        # Filter out trading classes that we don't want
        chain_flat = chain_flat[~chain_flat["tradingClass"].isin(self.excluded_trading_classes)]
        
        # Assign the filtered chain to the instance variable 
        self._chain_flat = chain_flat

        # Request the greeks for each option of the options chain
        # This operation can take a few seconds, and will perform better for
        # well-filtered chains using the max_strike_dist and max_dte parameters
        rights = ['P', 'C']
        updated_count = 0
        for _, opt in self._chain_flat.iterrows():
            for right in rights:
                # Do not fetch this ticker if we already have it!
                # TODO: Currently there is somewhat of a bug here...
                # There will be multiple options with the same key, since the
                # key does not factor in the tradingClass of the options.
                # I need to have a way to account for tradingClass.
                key = (opt["expiration"], opt["strike"], right.lower())
                if key in self._contract_tickers.keys():
                    continue
                
                updated_count += 1
                if write_status:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Requesting market data for {updated_count} of {len(self._chain_flat)*2} options...", end='\r', flush=True)
                    asyncio.sleep(0.01)
                
                # Create the contract object and fetch the ticker
                contract = Contract(
                    secType="FOP" if self.contract.secType == "FUT" else "OPT",
                    symbol=self.contract.symbol,
                    lastTradeDateOrContractMonth=opt['expiration'],
                    strike=opt['strike'],
                    right=right,
                    exchange=opt['exchange'],
                    multiplier=opt['multiplier'],
                    tradingClass=opt['tradingClass'],
                )
                await self._fetch_option_ticker(contract)
        
        if write_status:
            print(flush=True) # Don't print over the last line in the status counter
        
        # Always write the the option chain was updated
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Option chain updated; added {updated_count} contracts.", flush=True)
        
        
    async def _fetch_option_ticker(self, contract: Contract) -> None:
        async with asyncio.timeout(15):
            contract = (await self._ib.qualifyContractsAsync(contract))[0]
            ticker = self._ib.reqMktData(contract, snapshot=False, regulatorySnapshot=False)
            self._contract_tickers[(contract.lastTradeDateOrContractMonth, contract.strike, contract.right.lower())] = ticker
        

    def get(self, option: Contract, name: str) -> Ticker | None:
        key = (option.lastTradeDateOrContractMonth, option.strike, option.right.lower())
        if not key in self._contract_tickers:
            raise ValueError(f"Option with parameters {key} not in option chain!")
            
        if name in ["delta", "gamma", "vega", "theta", "iv"]:
            return getattr(self._contract_tickers.get(key).modelGreeks, name)
        
        if not hasattr(Ticker(), name):
            raise ValueError(f"Ticker has no attribute '{name}'.")
        return getattr(self._contract_tickers.get(key, None), name)


    def as_df(self) -> pd.DataFrame:
        if not len(self._contract_tickers):
            raise ValueError("No options contracts found in the chain.")
        
        # TODO: If we don't want to use modelGreeks, provide options to use other greek methods (bid, ask, last greeks)
        # Get most recent options price and greeks data from the streams
        calls_data, puts_data = [], []
        for (expiration, strike, right), ticker in self._contract_tickers.items():
            data = {
                    "expiration": expiration,
                    "strike": strike,
                    f"{right}_bid": ticker.bid,
                    f"{right}_ask": ticker.ask,
                    f"{right}_last": ticker.last,
                    f"{right}_volume": ticker.volume,
                    f"{right}_delta": ticker.modelGreeks.delta if ticker.modelGreeks else None,
                    f"{right}_gamma": ticker.modelGreeks.gamma if ticker.modelGreeks else None,
                    f"{right}_vega": ticker.modelGreeks.vega if ticker.modelGreeks else None,
                    f"{right}_theta": ticker.modelGreeks.theta if ticker.modelGreeks else None,
                    f"{right}_iv": ticker.modelGreeks.impliedVol if ticker.modelGreeks else None,
                }
            
            if right == 'c':
                calls_data.append(data)
            elif right == 'p':
                puts_data.append(data)

        # Convert data to DataFrame and merge into one flat DataFrame
        # Do not save this to the instance variable, as the data will soon
        # be out of date
        greeks_df = pd.merge(pd.DataFrame(calls_data), 
                             pd.DataFrame(puts_data), 
                             on=['expiration', 'strike'], 
                             how='outer')
        chain_flat = pd.merge(self._chain_flat, greeks_df, on=['expiration', 'strike'], how='left')
        return chain_flat
            
            
    def find_best_strike_by_delta(self, option_type: str, desired_exp: date, desired_delta: float) -> tuple[float, float]:
        chain = self.as_df()
        expiration = desired_exp.strftime("%Y%m%d")
        matched_exp = chain[chain["expiration"] == expiration]
        if len(matched_exp) == 0:
            return None, None, None
    
        delta_col = f"{option_type.lower()}_delta"
        id_best_delta = (matched_exp[delta_col] - desired_delta).abs().idxmin()
        best_delta = matched_exp.loc[id_best_delta]
        best_strike = best_delta['strike']
        return best_strike, best_delta[delta_col]