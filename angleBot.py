import streamlit as st
import pandas as pd
import asyncio
import websockets
import json
import threading
from datetime import datetime, timedelta
from smartapi import SmartConnect
import os

STATE_FILE = "bot_state_angelone.json"
WS_BASE_URL = "wss://marginsocket.angelbroking.com/smart-stream"

# --- State Persistence ---
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "candles": st.session_state.candles,
                "position": st.session_state.position,
                "traded_candle": str(st.session_state.traded_candle) if st.session_state.traded_candle else None
            }, f)
    except Exception as e:
        st.error(f"Error saving state: {e}")

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            st.session_state.candles = [
                {**c, "timestamp": pd.to_datetime(c["timestamp"])} for c in data.get("candles", [])
            ]
            st.session_state.position = data.get("position", None)
            traded = data.get("traded_candle")
            st.session_state.traded_candle = pd.to_datetime(traded) if traded else None
        else:
            st.session_state.candles = []
            st.session_state.position = None
            st.session_state.traded_candle = None
    except Exception as e:
        st.error(f"Error loading state: {e}")

# --- Utility functions (round_strike, get_option_instrument_token, update_candles) ---
def round_strike(price, interval=50):
    return int(price // interval * interval)

def get_option_instrument_token(strike, expiry_date, client: SmartConnect):
    try:
        instruments = client.searchInstruments(exchange="NFO", symbol="NIFTY")
    except Exception as e:
        st.warning(f"Error fetching instruments: {e}")
        return None, None
    for inst in instruments:
        if not ('expiry' in inst and 'strikeprice' in inst and 'optiontype' in inst):
            continue
        if (inst['expiry'].strftime("%Y-%m-%d") == expiry_date and
            inst['strikeprice'] == strike and
            inst['optiontype'].upper() == 'CE'):
            return inst['symboltoken'], inst['tradingsymbol']
    return None, None

def update_candles(ts, price, minutes=5):
    candles = st.session_state.candles
    start = ts - timedelta(minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond)
    if not candles or candles[-1]['timestamp'] != start:
        candles.append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    st.session_state.candles = candles

# --- Core Bot Tick Processing ---
async def on_tick(tick):
    try:
        ts = datetime.fromtimestamp(tick['timestamp'] / 1000)
        ltp = float(tick['lastprice'])
    except Exception:
        return

    update_candles(ts, ltp)
    save_state()

    df = pd.DataFrame(st.session_state.candles)
    if len(df) < 21:
        return

    df['ma10'] = df['close'].rolling(10).mean()
    df['ma21'] = df['close'].rolling(21).mean()
    last = df.iloc[-2]

    if (last['ma10'] >= last['ma21'] and
        st.session_state.traded_candle != last['timestamp'] and
        not st.session_state.position):

        strike = round_strike(last['close']) - 200
        opt_token, opt_symbol = get_option_instrument_token(strike, st.session_state.expiry_date, st.session_state.client)
        if not opt_token:
            st.warning("Option instrument not found.")
            return

        if st.session_state.paper_mode:
            entry_price = last['close']
            st.write(f"[PAPER] Bought {strike} CE @ {entry_price}")
        else:
            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": opt_symbol,
                "symboltoken": opt_token,
                "transactiontype": "BUY",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": st.session_state.lot_size
            }
            try:
                order_response = st.session_state.client.placeOrder(order_params)
                entry_price = order_response['data']['averageprice']
                st.write(f"Bought {strike} CE @ {entry_price}")
            except Exception as e:
                st.error(f"Buy failed: {e}")
                return

        st.session_state.position = {
            'option_token': opt_token,
            'tradingsymbol': opt_symbol,
            'entry_price': entry_price,
            'sl_price': entry_price * 0.95,
            'max_price': entry_price
        }
        st.session_state.traded_candle = last['timestamp']
        save_state()

    if st.session_state.position:
        try:
            if st.session_state.paper_mode:
                ltp_opt = st.session_state.position['max_price'] + 1
            else:
                quote = st.session_state.client.get_quotes("NFO", st.session_state.position['tradingsymbol'])
                ltp_opt = float(quote['data'][st.session_state.position['tradingsymbol']]['lastprice'])
        except Exception:
            return

        if ltp_opt > st.session_state.position['max_price']:
            st.session_state.position['max_price'] = ltp_opt
            st.session_state.position['sl_price'] = max(st.session_state.position['sl_price'], ltp_opt * 0.95)
            save_state()

        if ltp_opt <= st.session_state.position['sl_price']:
            if st.session_state.paper_mode:
                exit_price = ltp_opt
            else:
                order_params = {
                    "variety": "NORMAL",
                    "tradingsymbol": st.session_state.position['tradingsymbol'],
                    "symboltoken": st.session_state.position['option_token'],
                    "transactiontype": "SELL",
                    "exchange": "NFO",
                    "ordertype": "MARKET",
                    "producttype": "INTRADAY",
                    "duration": "DAY",
                    "quantity": st.session_state.lot_size
                }
                try:
                    sell_order = st.session_state.client.placeOrder(order_params)
                    exit_price = sell_order['data']['averageprice']
                except Exception as e:
                    st.error(f"Exit failed: {e}")
                    return

            pnl = (exit_price - st.session_state.position['entry_price']) * st.session_state.lot_size
            st.success(f"Trade exited. P&L = {pnl}")
            st.session_state.position = None
            save_state()

# --- WebSocket Background Listener ---
async def angelone_websocket_handler():
    api_key = st.session_state.api_key
    client = st.session_state.client
    access_token = client.generateSessionToken()

    async with websockets.connect(WS_BASE_URL) as websocket:
        # Authenticate
        await websocket.send(json.dumps({
            "action": "authenticate",
            "data": {"apiKey": api_key, "accessToken": access_token}
        }))
        auth_response = await websocket.recv()
        st.info(f"WebSocket Auth Response: {auth_response}")

        # Subscribe tokens (include your option tokens here)
        tokens_to_subscribe = [256265]  # Example NIFTY token; add options
        await websocket.send(json.dumps({
            "action": "subscribe",
            "instrumentToken": tokens_to_subscribe
        }))
        sub_response = await websocket.recv()
        st.info(f"Subscribe Response: {sub_response}")

        # Listen loop
        while True:
            msg = await websocket.recv()
            msg_data = json.loads(msg)
            if msg_data.get("type") == "m":
                ticks = msg_data.get("data", [])
                for tick in ticks:
                    asyncio.run_coroutine_threadsafe(on_tick(tick), st.session_state.loop)

def start_ws_listener():
    # Run WebSocket listener in separate thread to keep Streamlit responsive
    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        st.session_state.loop = loop
        loop.run_until_complete(angelone_websocket_handler())
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

# --- Streamlit UI and Bot Entry ---
def trading_bot_page():
    st.title("Angel One Nifty50 MA Bot")

    api_key = st.sidebar.text_input("API Key")
    user_id = st.sidebar.text_input("User ID")
    password = st.sidebar.text_input("Password", type="password")
    st.session_state.api_key = api_key

    expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
    lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
    paper_mode = st.sidebar.checkbox("Paper Mode (No real orders)", True)

    if "candles" not in st.session_state:
        load_state()

    st.session_state.lot_size = lot_size
    st.session_state.paper_mode = paper_mode
    st.session_state.expiry_date = expiry_date

    if st.button("Start Bot"):
        if not (api_key and user_id and password and expiry_date):
            st.error("Fill all API & config fields")
            return

        st.session_state.client = SmartConnect(api_key)
        st.session_state.client.generateSession(user_id, password)

        start_ws_listener()
        st.success("Bot started and listening to WebSocket!")

def main():
    st.sidebar.title("Navigation")
    page = st.sidebar.radio("Go to", ["Trading Bot"])
    if page == "Trading Bot":
        trading_bot_page()

if __name__ == "__main__":
    main()
