print("BOT FILE RUNNING...")
import os
import time
import json
import math
import hmac
import hashlib
import requests
import traceback
from datetime import datetime, timedelta, timezone

# =========================
# CONFIG
# =========================

BASE_URL = "https://api.india.delta.exchange"

PERP_SYMBOL = "ETHUSD"
UNDERLYING = "ETH"

STRIKE_STEP = 20
THRESHOLD = float(os.getenv("THRESHOLD", "0.005"))   # 0.5% default
LOT_SIZE = float(os.getenv("LOT_SIZE", "1"))

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

SLEEP_SECONDS = 15
STATE_FILE = "state.json"

IST = timezone(timedelta(hours=5, minutes=30))

USER_AGENT = "eth-daily-strategy-bot"

print("BOT FILE RUNNING...")
print("BOT STARTED...")
print("PERP_SYMBOL:", PERP_SYMBOL)
print("UNDERLYING:", UNDERLYING)
print("LOT_SIZE:", LOT_SIZE)
print("STRIKE_STEP:", STRIKE_STEP)
print("THRESHOLD:", THRESHOLD)

if not DELTA_API_KEY or not DELTA_API_SECRET:
    raise Exception("Missing DELTA_API_KEY / DELTA_API_SECRET in environment!")

# =========================
# STATE MANAGEMENT
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "batches": [],
            "last_daily_run_date": None
        }

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "batches": [],
            "last_daily_run_date": None
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =========================
# API SIGNATURE HELPERS
# =========================

def generate_signature(message: str) -> str:
    return hmac.new(
        DELTA_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()


def private_get(endpoint: str, params=None):
    if params is None:
        params = {}

    query_string = ""
    if params:
        query_string = "?" + "&".join([f"{k}={v}" for k, v in params.items()])

    full_endpoint = endpoint + query_string
    url = BASE_URL + full_endpoint

    timestamp = str(int(time.time()))
    signature_data = "GET" + timestamp + full_endpoint
    signature = generate_signature(signature_data)

    headers = {
        "Accept": "application/json",
        "api-key": DELTA_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.get(url, headers=headers, timeout=20)
    return r.json()


def private_post(endpoint: str, payload: dict):
    url = BASE_URL + endpoint
    timestamp = str(int(time.time()))
    body = json.dumps(payload)

    signature_data = "POST" + timestamp + endpoint + body
    signature = generate_signature(signature_data)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "api-key": DELTA_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.post(url, headers=headers, data=body, timeout=20)
    return r.json()


# =========================
# MARKET HELPERS
# =========================

def get_live_price():
    url = f"{BASE_URL}/v2/tickers/{PERP_SYMBOL}"
    r = requests.get(url, timeout=10)
    data = r.json()

    if data.get("success") is not True:
        raise Exception("Ticker API failed: " + str(data))

    return float(data["result"]["close"])


def get_perp_position_size():
    data = private_get("/v2/positions/margined")

    if data.get("success") is not True:
        raise Exception("Positions API failed: " + str(data))

    for p in data.get("result", []):
        if p.get("product_symbol") == PERP_SYMBOL:
            return float(p.get("size", 0))

    return 0.0


def get_last_buy_fill_price():
    """
    Return last BUY fill price for PERP_SYMBOL
    """
    data = private_get("/v2/fills", params={"page_size": 200})

    if data.get("success") is not True:
        raise Exception("Fills API failed: " + str(data))

    fills = data.get("result", [])
    for f in fills:
        if f.get("product_symbol") == PERP_SYMBOL and f.get("side") == "buy":
            return float(f.get("price"))

    return None


def get_open_orders():
    data = private_get("/v2/orders", params={"state": "open", "page_size": 200})
    if data.get("success") is not True:
        return []
    return data.get("result", [])


def place_market_order(symbol: str, side: str, size: float):
    payload = {
        "product_symbol": symbol,
        "size": size,
        "side": side,
        "order_type": "market_order"
    }
    res = private_post("/v2/orders", payload)
    print("ORDER RESPONSE:", res)
    return res


# =========================
# OPTION HELPERS
# =========================

def get_tomorrow_expiry_code():
    now_ist = datetime.now(IST)
    tomorrow = now_ist + timedelta(days=1)
    return tomorrow.strftime("%d%m%y")


def get_atm_strike(price: float):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def make_call_symbol(strike: int, expiry_code: str):
    return f"C-{UNDERLYING}-{strike}-{expiry_code}"


# =========================
# GOLD-LIKE SAFE VERIFY
# =========================

def safe_recover_batches(state_batches, exchange_pos_size, exchange_last_buy):
    """
    If state empty but exchange position exists -> rebuild minimal batches.
    """
    if exchange_pos_size <= 0:
        return []

    if state_batches and len(state_batches) > 0:
        return state_batches

    # rebuild from last buy fill only (minimal safe)
    if exchange_last_buy is None:
        return []

    return [{
        "buy_price": exchange_last_buy,
        "size": exchange_pos_size,
        "hedge_symbol": None,
        "hedge_size": 0.0
    }]


# =========================
# STRATEGY CORE LOGIC
# =========================

def close_last_batch(state, live_price):
    """
    Close last batch:
    - Sell PERP (same size)
    - Buyback hedge call (if exists)
    """
    if not state["batches"]:
        return

    last = state["batches"][-1]

    size = float(last["size"])
    hedge_symbol = last.get("hedge_symbol")
    hedge_size = float(last.get("hedge_size", 0))

    print("CLOSING LAST BATCH -> buy_price:", last["buy_price"], "size:", size)

    # SELL PERP
    resp = place_market_order(PERP_SYMBOL, "sell", size)
    if resp.get("success") is not True:
        print("FAILED TO SELL PERP")
        return

    # BUY BACK HEDGE
    if hedge_symbol and hedge_size > 0:
        print("BUYBACK HEDGE:", hedge_symbol, hedge_size)
        resp2 = place_market_order(hedge_symbol, "buy", hedge_size)
        if resp2.get("success") is not True:
            print("FAILED TO BUYBACK HEDGE (MANUAL CHECK REQUIRED)")

    # remove batch
    state["batches"].pop()
    save_state(state)
    print("LAST BATCH CLOSED SUCCESSFULLY.")


def hedge_for_active_batches(state, live_price):
    """
    Hedge rule:
    Only hedge batches where buy_price <= live_price (price above their buy).
    Total those lots and sell ATM call (tomorrow expiry) for that many lots.
    """
    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    eligible_size = 0.0
    for b in state["batches"]:
        if live_price >= float(b["buy_price"]):
            eligible_size += float(b["size"])

    if eligible_size <= 0:
        print("NO ELIGIBLE BATCHES FOR HEDGE SELL.")
        return

    print("HEDGE REQUIRED -> eligible lots:", eligible_size, "symbol:", call_symbol)

    # SELL CALL OPTION
    resp = place_market_order(call_symbol, "sell", eligible_size)
    if resp.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL")
        return

    # mark hedge on LAST batch only (tracking purpose)
    state["batches"][-1]["hedge_symbol"] = call_symbol
    state["batches"][-1]["hedge_size"] = eligible_size
    save_state(state)

    print("HEDGE SOLD SUCCESSFULLY:", call_symbol, eligible_size)


def add_new_buy_batch(state, live_price):
    """
    Add new buy batch:
    - Buy PERP LOT_SIZE
    - Sell ATM call tomorrow expiry for LOT_SIZE
    """
    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    print("NEW BUY BATCH -> BUY PERP:", LOT_SIZE, "at price:", live_price)

    resp = place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
    if resp.get("success") is not True:
        print("FAILED TO BUY PERP")
        return

    # use exchange last buy fill as true buy price
    true_buy_price = get_last_buy_fill_price()
    if true_buy_price is None:
        true_buy_price = live_price

    print("SELLING HEDGE CALL:", call_symbol, LOT_SIZE)

    resp2 = place_market_order(call_symbol, "sell", LOT_SIZE)
    if resp2.get("success") is not True:
        print("FAILED TO SELL CALL HEDGE")
        return

    state["batches"].append({
        "buy_price": true_buy_price,
        "size": LOT_SIZE,
        "hedge_symbol": call_symbol,
        "hedge_size": LOT_SIZE
    })

    save_state(state)
    print("NEW BATCH ADDED:", true_buy_price, LOT_SIZE)


def daily_execute(state):
    """
    Executes once per day at 3:30 PM IST.
    """
    live_price = get_live_price()
    pos_size = get_perp_position_size()

    print("DAILY EXECUTION START -> LIVE:", live_price, "POS:", pos_size)

    if pos_size <= 0:
        print("NO POSITION FOUND. DAILY STRATEGY SKIPPED.")
        return

    # if no batches exist, initialize from last buy fill
    if not state["batches"]:
        last_buy = get_last_buy_fill_price()
        if last_buy:
            state["batches"].append({
                "buy_price": last_buy,
                "size": pos_size,
                "hedge_symbol": None,
                "hedge_size": 0.0
            })
            save_state(state)
            print("INITIALIZED BATCH FROM EXCHANGE LAST BUY:", last_buy)

    # LOOP: close batches if price >= last batch buy price
    while state["batches"]:
        last_price = float(state["batches"][-1]["buy_price"])

        if live_price >= last_price:
            print("PRICE ABOVE LAST BATCH -> closing...")
            close_last_batch(state, live_price)
            live_price = get_live_price()
            continue
        break

    if not state["batches"]:
        print("ALL BATCHES CLOSED. STRATEGY END.")
        return

    # after closures, check last batch threshold
    last_batch_price = float(state["batches"][-1]["buy_price"])
    down_trigger = last_batch_price * (1 - THRESHOLD)

    if live_price <= down_trigger:
        print("PRICE DOWN >= THRESHOLD -> NEW BUY + HEDGE")
        add_new_buy_batch(state, live_price)
    else:
        print("PRICE NOT DOWN ENOUGH -> ONLY HEDGE SELL REQUIRED")
        hedge_for_active_batches(state, live_price)

    print("DAILY EXECUTION DONE.")


# =========================
# STARTUP RECOVERY
# =========================

state = load_state()
print("RECOVERY STARTED...")

try:
    pos_size = get_perp_position_size()
    last_buy = get_last_buy_fill_price()
    live_price = get_live_price()

    print("EXCHANGE PERP POSITION SIZE:", pos_size)
    print("EXCHANGE LAST BUY FILL:", last_buy)

    state["batches"] = safe_recover_batches(state.get("batches", []), pos_size, last_buy)
    save_state(state)

    print("RECOVERY DONE. BATCHES:", state["batches"])

except Exception as e:
    print("RECOVERY ERROR:", str(e))
    traceback.print_exc()


# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now_ist = datetime.now(IST)

        # 3:30 PM window
        if now_ist.hour == 15 and now_ist.minute == 30:
            today_str = now_ist.strftime("%Y-%m-%d")

            if state.get("last_daily_run_date") != today_str:
                print("3:30 PM IST TRIGGERED. Running daily strategy...")
                daily_execute(state)
                state["last_daily_run_date"] = today_str
                save_state(state)
            else:
                print("Already executed today. Waiting next day...")

            time.sleep(60)

        else:
            time.sleep(SLEEP_SECONDS)

    except Exception as e:
        print("MAIN LOOP ERROR:", str(e))
        traceback.print_exc()
        time.sleep(SLEEP_SECONDS)
