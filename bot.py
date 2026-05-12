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

PERP_SYMBOL = os.getenv("PERP_SYMBOL", "ETHUSD")   # ETH perpetual
UNDERLYING = os.getenv("UNDERLYING", "ETH")        # ETH for option format

STRIKE_STEP = int(os.getenv("STRIKE_STEP", "20"))  # ETH option strike gap = 20
LOT_SIZE = float(os.getenv("LOT_SIZE", "0.01"))    # Render env lot size
CHECK_HOUR = int(os.getenv("CHECK_HOUR", "15"))    # 3:30 PM IST
CHECK_MINUTE = int(os.getenv("CHECK_MINUTE", "30"))

THRESHOLD = float(os.getenv("THRESHOLD", "0.005")) # 0.5%

STATE_FILE = "state.json"

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

USER_AGENT = "eth-daily-strategy-bot"

SLEEP_SECONDS = 10

IST = timezone(timedelta(hours=5, minutes=30))

print("BOT STARTED...")
print("PERP_SYMBOL:", PERP_SYMBOL)
print("UNDERLYING:", UNDERLYING)
print("LOT_SIZE:", LOT_SIZE)
print("STRIKE_STEP:", STRIKE_STEP)
print("THRESHOLD:", THRESHOLD)

if not API_KEY or not API_SECRET:
    raise Exception("DELTA_API_KEY / DELTA_API_SECRET missing in Render Environment!")

# =========================
# SIGNATURE HELPERS
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
# MARKET DATA
# =========================

def get_live_price(symbol):
    url = f"{BASE_URL}/v2/tickers/{symbol}"
    r = requests.get(url, timeout=15)
    data = r.json()
    if data.get("success") is not True:
        raise Exception("Ticker failed: " + str(data))
    return float(data["result"]["close"])

# =========================
# POSITIONS
# =========================

def get_open_positions():
    data = private_get("/v2/positions/margined")
    if data.get("success") is not True:
        raise Exception("Positions API failed: " + str(data))
    return data.get("result", [])

def get_position_size(symbol):
    positions = get_open_positions()
    for p in positions:
        if p.get("product_symbol") == symbol:
            return float(p.get("size", 0))
    return 0.0

def get_all_option_positions():
    positions = get_open_positions()
    opts = []
    for p in positions:
        sym = p.get("product_symbol", "")
        if sym.startswith("C-") or sym.startswith("P-"):
            if abs(float(p.get("size", 0))) > 0:
                opts.append(p)
    return opts

# =========================
# FILLS
# =========================

def get_last_fill_price(symbol):
    data = private_get("/v2/fills", params={"page_size": 100})
    if data.get("success") is not True:
        raise Exception("Fills API failed: " + str(data))

    fills = data.get("result", [])
    for f in fills:
        if f.get("product_symbol") == symbol:
            return float(f.get("price"))
    return None

# =========================
# ORDER PLACEMENT
# =========================

def place_market_order(symbol: str, side: str, size: float):
    payload = {
        "product_symbol": symbol,
        "size": size,
        "side": side,
        "order_type": "market_order"
    }
    res = private_post("/v2/orders", payload)
    print("ORDER:", side, symbol, size, "->", res)
    return res

# =========================
# STATE FILE
# =========================

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "batches": [],
            "hedge_symbol": None,
            "hedge_lots": 0,
            "last_run_date": None
        }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# =========================
# OPTION HELPERS
# =========================

def format_expiry_code(dt: datetime):
    # Delta format example: 130526 (DDMMYY)
    return dt.strftime("%d%m%y")

def get_tomorrow_expiry_code():
    now = datetime.now(IST)
    tomorrow = now + timedelta(days=1)
    return format_expiry_code(tomorrow)

def get_today_expiry_code():
    now = datetime.now(IST)
    return format_expiry_code(now)

def round_to_atm_strike(price):
    # nearest STRIKE_STEP
    strike = round(price / STRIKE_STEP) * STRIKE_STEP
    return int(strike)

def build_call_symbol(strike, expiry_code):
    # format: C-ETH-2320-130526
    return f"C-{UNDERLYING}-{strike}-{expiry_code}"

# =========================
# RECOVERY ON STARTUP
# =========================

def recover_state_from_exchange(state):
    print("RECOVERY STARTED...")

    perp_size = get_position_size(PERP_SYMBOL)
    print("EXCHANGE PERP POSITION SIZE:", perp_size)

    # if no batches but perp exists -> recover last fill as one batch
    if perp_size > 0 and len(state["batches"]) == 0:
        last_fill = get_last_fill_price(PERP_SYMBOL)
        if last_fill:
            lots = perp_size
            state["batches"].append({
                "price": last_fill,
                "lots": lots
            })
            print("RECOVERED BATCH:", last_fill, lots)

    # detect any existing CALL short
    options = get_all_option_positions()
    for op in options:
        sym = op.get("product_symbol", "")
        size = float(op.get("size", 0))
        if sym.startswith("C-") and size < 0:
            state["hedge_symbol"] = sym
            state["hedge_lots"] = abs(size)
            print("RECOVERED HEDGE:", sym, abs(size))
            break

    save_state(state)
    print("RECOVERY DONE.")
    return state

# =========================
# STRATEGY LOGIC
# =========================

def close_hedge_if_expiring_today(state):
    hedge_sym = state.get("hedge_symbol")
    hedge_lots = float(state.get("hedge_lots", 0))

    if not hedge_sym or hedge_lots <= 0:
        return

    today_code = get_today_expiry_code()

    # hedge symbol ends with -DDMMYY
    if hedge_sym.endswith(today_code):
        print("HEDGE EXPIRING TODAY -> BUYBACK:", hedge_sym)
        place_market_order(hedge_sym, "buy", hedge_lots)
        state["hedge_symbol"] = None
        state["hedge_lots"] = 0
        save_state(state)

def ensure_tomorrow_hedge(state, target_lots, live_price):
    expiry_code = get_tomorrow_expiry_code()
    strike = round_to_atm_strike(live_price)
    call_sym = build_call_symbol(strike, expiry_code)

    existing_sym = state.get("hedge_symbol")
    existing_lots = float(state.get("hedge_lots", 0))

    # If hedge already correct -> do nothing
    if existing_sym == call_sym and abs(existing_lots - target_lots) < 1e-9:
        print("HEDGE OK:", existing_sym, existing_lots)
        return

    # If hedge exists but wrong -> close old hedge first
    if existing_sym and existing_lots > 0:
        print("CLOSING OLD HEDGE:", existing_sym, existing_lots)
        place_market_order(existing_sym, "buy", existing_lots)

    # Sell new hedge
    if target_lots > 0:
        print("SELLING NEW HEDGE:", call_sym, target_lots)
        place_market_order(call_sym, "sell", target_lots)

        state["hedge_symbol"] = call_sym
        state["hedge_lots"] = target_lots
        save_state(state)

def get_last_batch(state):
    if len(state["batches"]) == 0:
        return None
    return state["batches"][-1]

def pop_last_batch(state):
    if len(state["batches"]) > 0:
        return state["batches"].pop()
    return None

def strategy_run(state):
    live_price = get_live_price(PERP_SYMBOL)
    print("LIVE PRICE:", live_price)

    # If no batches -> first buy
    if len(state["batches"]) == 0:
        print("NO BATCH FOUND -> FIRST BUY")
        place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
        state["batches"].append({"price": live_price, "lots": LOT_SIZE})
        save_state(state)

    # Step 1: If hedge expiring today -> close it
    close_hedge_if_expiring_today(state)

    # Step 2: Loop for price >= last batch price (close batch)
    while True:
        last_batch = get_last_batch(state)
        if not last_batch:
            break

        last_price = float(last_batch["price"])
        last_lots = float(last_batch["lots"])

        live_price = get_live_price(PERP_SYMBOL)
        print("CHECK BATCH:", last_price, "LIVE:", live_price)

        if live_price >= last_price:
            print("PRICE >= LAST BATCH -> CLOSE LAST BATCH (SELL + BUYBACK CALL)")
            # close hedge before closing batch
            if state.get("hedge_symbol") and state.get("hedge_lots") > 0:
                place_market_order(state["hedge_symbol"], "buy", state["hedge_lots"])
                state["hedge_symbol"] = None
                state["hedge_lots"] = 0

            # close BTC/ETH batch
            place_market_order(PERP_SYMBOL, "sell", last_lots)
            pop_last_batch(state)
            save_state(state)

            # continue loop to check next last batch
            continue

        break

    # Step 3: After closing possible batches, decide next action
    last_batch = get_last_batch(state)
    if not last_batch:
        print("ALL BATCHES CLOSED -> KEEP 1 BASE POSITION")
        place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
        state["batches"].append({"price": live_price, "lots": LOT_SIZE})
        save_state(state)
        last_batch = get_last_batch(state)

    last_price = float(last_batch["price"])
    last_lots = float(last_batch["lots"])

    live_price = get_live_price(PERP_SYMBOL)
    down_trigger = last_price * (1 - THRESHOLD)

    print("FINAL CHECK -> LAST:", last_price, "DOWN_TRIGGER:", down_trigger, "LIVE:", live_price)

    if live_price <= down_trigger:
        print("0.5% DOWN OR MORE -> NEW BUY + NEW TOMORROW HEDGE")
        place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
        state["batches"].append({"price": live_price, "lots": LOT_SIZE})
        save_state(state)

        # hedge only against new last batch lots
        ensure_tomorrow_hedge(state, LOT_SIZE, live_price)

    else:
        print("NO NEW BUY -> ONLY TOMORROW ATM HEDGE AGAINST LAST BATCH")
        ensure_tomorrow_hedge(state, last_lots, live_price)

    return state

# =========================
# MAIN LOOP (Daily 3:30 PM IST)
# =========================

state = load_state()
state = recover_state_from_exchange(state)

while True:
    try:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")

        if now.hour == CHECK_HOUR and now.minute == CHECK_MINUTE:
            if state.get("last_run_date") != today_str:
                print("===================================")
                print("3:30 PM IST TRIGGER -> RUNNING STRATEGY")
                print("===================================")

                state = load_state()  # reload latest state
                state = recover_state_from_exchange(state)

                state = strategy_run(state)

                state["last_run_date"] = today_str
                save_state(state)

                print("STRATEGY COMPLETED FOR:", today_str)

            else:
                print("ALREADY RAN TODAY:", today_str)

        time.sleep(SLEEP_SECONDS)

    except Exception as e:
        print("RUNTIME ERROR:", str(e))
        traceback.print_exc()
        time.sleep(10)
