import asyncio
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
        self._host: str = "127.0.0.1"
        self._port: int = 7496
        self._client_id: int = 1
        self._account_id: str = ""

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
        weekday_only: bool = True,
    ):
        self._day_start_time = day_start_time
        self._day_stop_time = day_stop_time
        self._tick_freq_seconds = tick_freq_seconds
        self._weekday_only = weekday_only
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account_id = account_id
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

                # Check connection health and attempt reconnect if needed
                if not self.ib.isConnected():
                    print("[Strategy] Connection lost. Attempting to reconnect...")
                    try:
                        self.ib.disconnect()
                    except Exception:
                        pass
                    self.ib = IB()
                    try:
                        self.ib.connect(
                            host=self._host,
                            port=self._port,
                            clientId=self._client_id,
                            account=self._account_id,
                        )
                        if self.ib.isConnected():
                            print("[Strategy] Reconnected to IB.")
                            await self.on_reconnect()
                    except Exception as e:
                        print(f"[Strategy] Reconnect failed: {e}")
                        await asyncio.sleep(30)
                        continue
                    if not self.ib.isConnected():
                        await asyncio.sleep(30)
                        continue

                # Check for mode transitions
                await self._check_mode_transition()

                # Only call tick when in live mode
                if self._is_live:
                    await self.tick()
                    await asyncio.sleep(self._tick_freq_seconds)
                else:
                    print(f"Strategy is in sleep mode between {self._day_stop_time} and {self._day_start_time}")
                    await asyncio.sleep(self._tick_freq_seconds)

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

    async def on_reconnect(self):
        """Called when the strategy successfully reconnects to IB Gateway after a disconnect.
        Override in subclass to re-establish subscriptions or reset state as needed."""
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
