import asyncio

from ib_async import IB


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

