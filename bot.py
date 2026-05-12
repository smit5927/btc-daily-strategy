import os
import time
import json
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

LOT_SIZE = int(os.getenv("LOT_SIZE", "1"))
SLEEP_SECONDS = 5

# Daily trigger time (IST)
CHECK_HOUR = 15
CHECK_MINUTE = 30
IST_OFFSET = 5.5

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
USER_AGENT = "eth-daily-hedge-bot"

print("BOT STARTED...")
print("API KEY LOADED:", API_KEY is not None)
print("API SECRET LOADED:", API_SECRET is not None)
print("PERP_SYMBOL:", PERP_SYMBOL)
print("LOT_SIZE:", LOT_SIZE)

# =========================
# API SIGNATURE HELPERS
# =========================

def generate_signature(message: str) -> str:
    return hmac.new(
        API_SECRET.encode(),
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
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.get(url, headers=headers, timeout=15)
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
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": USER_AGENT
    }

    r = requests.post(url, headers=headers, data=body, timeout=15)
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


def round_to_step(price, step):
    return int(round(price / step) * step)


def get_tomorrow_expiry_code():
    ist = timezone(timedelta(hours=IST_OFFSET))
    now_ist = datetime.now(ist)
    tomorrow = now_ist + timedelta(days=1)
    return tomorrow.strftime("%d%m%y")  # 130526 format


def get_tomorrow_atm_call_symbol(price):
    strike = round_to_step(price, STRIKE_STEP)
    expiry_code = get_tomorrow_expiry_code()
    symbol = f"C-{UNDERLYING}-{strike}-{expiry_code}"
    return symbol, strike, expiry_code

# =========================
# ORDERS
# =========================

def place_market_order(symbol, side, size):
    payload = {
        "product_symbol": symbol,
        "size": int(size),
        "side": side,
        "order_type": "market_order"
    }
    res = private_post("/v2/orders", payload)
    print("ORDER:", side, symbol, size, "=>", res)
    return res


# =========================
# STATE FILE
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "batches": [],
            "sold_call_symbol": None,
            "sold_call_lots": 0,
            "last_run_date": None
        }

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "batches": [],
            "sold_call_symbol": None,
            "sold_call_lots": 0,
            "last_run_date": None
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# =========================
# STRATEGY HELPERS
# =========================

def count_profit_lots(batches, live_price):
    profit = 0
    for b in batches:
        if float(b["buy_price"]) < live_price:
            profit += int(b["lot"])
    return profit


def close_last_batch(state):
    last_batch = state["batches"][-1]
    last_lot = int(last_batch["lot"])

    print("CLOSING LAST BATCH:", last_batch)

    # SELL PERP
    place_market_order(PERP_SYMBOL, "sell", last_lot)

    # remove batch
    state["batches"].pop()


def adjust_call_hedge(state, live_price):
    profit_lots = count_profit_lots(state["batches"], live_price)

    print("PROFIT LOTS (need hedge):", profit_lots)

    current_sold_lots = int(state.get("sold_call_lots", 0))
    current_symbol = state.get("sold_call_symbol")

    new_symbol, strike, expiry_code = get_tomorrow_atm_call_symbol(live_price)

    print("TOMORROW ATM CALL:", new_symbol, "STRIKE:", strike, "EXP:", expiry_code)

    # If symbol changed (new day), roll old hedge
    if current_symbol is not None and current_symbol != new_symbol and current_sold_lots > 0:
        print("ROLLING OLD CALL SYMBOL:", current_symbol, "->", new_symbol)

        # buyback old
        place_market_order(current_symbol, "buy", current_sold_lots)

        current_sold_lots = 0
        current_symbol = None

    # Adjust lots
    if current_sold_lots < profit_lots:
        diff = profit_lots - current_sold_lots
        print("SELL EXTRA CALL LOTS:", diff)
        place_market_order(new_symbol, "sell", diff)
        current_sold_lots += diff
        current_symbol = new_symbol

    elif current_sold_lots > profit_lots:
        diff = current_sold_lots - profit_lots
        print("BUYBACK EXTRA CALL LOTS:", diff)
        place_market_order(new_symbol, "buy", diff)
        current_sold_lots -= diff
        if current_sold_lots == 0:
            current_symbol = None
        else:
            current_symbol = new_symbol

    state["sold_call_symbol"] = current_symbol
    state["sold_call_lots"] = current_sold_lots


# =========================
# MAIN DAILY STRATEGY
# =========================

def run_strategy_once():
    state = load_state()

    ist = timezone(timedelta(hours=IST_OFFSET))
    today = str(datetime.now(ist).date())

    if state.get("last_run_date") == today:
        print("Already executed today. Skipping...")
        return

    live_price = get_live_price()
    print("LIVE ETH PRICE:", live_price)

    # FIRST BUY IF NO BATCHES
    if len(state["batches"]) == 0:
        print("NO POSITION -> FIRST BUY")

        place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)

        state["batches"].append({
            "buy_price": live_price,
            "lot": LOT_SIZE
        })

        adjust_call_hedge(state, live_price)

        state["last_run_date"] = today
        save_state(state)
        return

    # ================================
    # LOOP: CLOSE PROFIT BATCHES
    # Keep minimum 1 batch always
    # ================================
    while len(state["batches"]) > 1:
        last_batch = state["batches"][-1]
        last_price = float(last_batch["buy_price"])

        if live_price >= last_price:
            print("PRICE >= LAST BUY -> CLOSE LAST BATCH")
            close_last_batch(state)
        else:
            break

    # ================================
    # AFTER CLOSE LOOP, CHECK 0.5% DOWN
    # ================================
    last_batch = state["batches"][-1]
    last_price = float(last_batch["buy_price"])

    down_trigger = last_price * (1 - 0.005)

    if live_price <= down_trigger:
        print("PRICE DOWN >= 0.5% -> NEW BUY")

        place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)

        state["batches"].append({
            "buy_price": live_price,
            "lot": LOT_SIZE
        })
    else:
        print("PRICE NOT DOWN 0.5% -> NO NEW BUY")

    # ================================
    # ADJUST HEDGE (profit lots only)
    # ================================
    adjust_call_hedge(state, live_price)

    state["last_run_date"] = today
    save_state(state)


# =========================
# 24/7 LOOP
# =========================

while True:
    try:
        ist = timezone(timedelta(hours=IST_OFFSET))
        now = datetime.now(ist)

        if now.hour == CHECK_HOUR and now.minute == CHECK_MINUTE:
            print("3:30 PM IST TRIGGERED -> RUNNING STRATEGY")
            run_strategy_once()

            # sleep 70 seconds to avoid double trigger same minute
            time.sleep(70)

        time.sleep(SLEEP_SECONDS)

    except Exception as e:
        print("ERROR:", str(e))
        traceback.print_exc()
        time.sleep(10)
