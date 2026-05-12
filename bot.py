import os
import sys
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

STRIKE_STEP = int(os.getenv("STRIKE_STEP", "20"))
THRESHOLD = float(os.getenv("THRESHOLD", "0.005"))  # 0.5%
LOT_SIZE = float(os.getenv("LOT_SIZE", "1"))

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"

SLEEP_SECONDS = 10
IST = timezone(timedelta(hours=5, minutes=30))

USER_AGENT = "eth-daily-strategy-bot"

print("BOT FILE RUNNING...")
sys.stdout.flush()

print("BOT STARTED...")
print("PERP_SYMBOL:", PERP_SYMBOL)
print("UNDERLYING:", UNDERLYING)
print("LOT_SIZE:", LOT_SIZE)
print("STRIKE_STEP:", STRIKE_STEP)
print("THRESHOLD:", THRESHOLD)
sys.stdout.flush()

if not DELTA_API_KEY or not DELTA_API_SECRET:
    raise Exception("Missing DELTA_API_KEY / DELTA_API_SECRET in environment!")

# =========================
# SAFE REQUEST JSON
# =========================

def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        print("BAD RESPONSE TEXT:", resp.text[:500])
        sys.stdout.flush()
        raise

# =========================
# LOCK SYSTEM
# =========================

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        print("LOCK FILE EXISTS -> BOT ALREADY RUNNING. EXITING.")
        sys.stdout.flush()
        sys.exit(0)

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    print("LOCK ACQUIRED.")
    sys.stdout.flush()

def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except:
        pass

acquire_lock()

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
    return safe_json(r)

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
    return safe_json(r)

# =========================
# MARKET HELPERS
# =========================

def get_live_price():
    url = f"{BASE_URL}/v2/tickers/{PERP_SYMBOL}"
    r = requests.get(url, timeout=10)
    data = safe_json(r)

    if data.get("success") is not True:
        raise Exception("Ticker API failed: " + str(data))

    return float(data["result"]["close"])

def get_positions():
    data = private_get("/v2/positions/margined")

    if data.get("success") is not True:
        raise Exception("Positions API failed: " + str(data))

    return data.get("result", [])

def get_perp_position_size():
    positions = get_positions()

    for p in positions:
        if p.get("product_symbol") == PERP_SYMBOL:
            return float(p.get("size", 0))

    return 0.0

def get_open_option_positions():
    positions = get_positions()
    option_positions = []

    for p in positions:
        sym = p.get("product_symbol")
        if sym and sym.startswith("C-") and f"-{UNDERLYING}-" in sym:
            size = float(p.get("size", 0))
            if abs(size) > 0:
                option_positions.append({"symbol": sym, "size": size})

    return option_positions

def place_market_order(symbol: str, side: str, size: float):
    payload = {
        "product_symbol": symbol,
        "size": size,
        "side": side,
        "order_type": "market_order"
    }

    res = private_post("/v2/orders", payload)
    print("ORDER RESPONSE:", res)
    sys.stdout.flush()
    return res

# =========================
# OPTION HELPERS
# =========================

def get_today_expiry_code():
    now_ist = datetime.now(IST)
    return now_ist.strftime("%d%m%y")

def get_tomorrow_expiry_code():
    now_ist = datetime.now(IST)
    tomorrow = now_ist + timedelta(days=1)
    return tomorrow.strftime("%d%m%y")

def get_atm_strike(price: float):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)

def make_call_symbol(strike: int, expiry_code: str):
    return f"C-{UNDERLYING}-{strike}-{expiry_code}"

def extract_expiry_code(option_symbol: str):
    # Example: C-ETH-2340-120526
    parts = option_symbol.split("-")
    if len(parts) >= 4:
        return parts[-1]
    return None

# =========================
# RECOVERY (STATE + EXCHANGE VERIFY)
# =========================

def recover_state_from_exchange(state):
    print("RECOVERY STARTED...")
    sys.stdout.flush()

    pos_size = get_perp_position_size()
    live_price = get_live_price()

    option_positions = get_open_option_positions()

    print("EXCHANGE PERP POSITION SIZE:", pos_size)
    print("EXCHANGE OPTION POSITIONS:", option_positions)
    print("STARTUP LIVE PRICE:", live_price)
    sys.stdout.flush()

    # If no perp position -> wipe batches
    if pos_size <= 0:
        state["batches"] = []
        save_state(state)
        print("NO PERP POSITION -> STATE RESET.")
        sys.stdout.flush()
        return state

    # If batches exist -> keep but verify total size
    if state.get("batches") and len(state["batches"]) > 0:
        total_batch_size = sum(float(b["size"]) for b in state["batches"])

        # if mismatch too large -> rebuild
        if abs(total_batch_size - pos_size) > 0.0001:
            print("STATE MISMATCH WITH EXCHANGE -> REBUILDING BATCHES.")
            sys.stdout.flush()
            state["batches"] = []

    # If no batches, create a single batch at live price approximation
    if not state.get("batches") or len(state["batches"]) == 0:
        state["batches"] = [{
            "buy_price": live_price,
            "size": pos_size,
            "hedge_symbol": None,
            "hedge_size": 0.0
        }]

    # If option sell exists, attach it to last batch
    for op in option_positions:
        if op["size"] < 0:
            state["batches"][-1]["hedge_symbol"] = op["symbol"]
            state["batches"][-1]["hedge_size"] = abs(float(op["size"]))

    save_state(state)

    print("RECOVERY DONE. BATCHES:", state["batches"])
    sys.stdout.flush()

    return state

# =========================
# STRATEGY CORE LOGIC
# =========================

def close_today_expiry_hedges_if_any():
    today_code = get_today_expiry_code()
    option_positions = get_open_option_positions()

    for op in option_positions:
        sym = op["symbol"]
        size = float(op["size"])

        expiry = extract_expiry_code(sym)

        # only close SELL positions (negative size)
        if size < 0 and expiry == today_code:
            print("TODAY EXPIRY HEDGE FOUND -> BUYBACK:", sym, "size:", abs(size))
            sys.stdout.flush()
            place_market_order(sym, "buy", abs(size))
            time.sleep(1)

def close_last_batch(state):
    if not state["batches"]:
        return

    last = state["batches"][-1]
    size = float(last["size"])
    hedge_symbol = last.get("hedge_symbol")
    hedge_size = float(last.get("hedge_size", 0))

    print("CLOSING LAST BATCH -> buy_price:", last["buy_price"], "size:", size)
    sys.stdout.flush()

    resp = place_market_order(PERP_SYMBOL, "sell", size)
    if resp.get("success") is not True:
        print("FAILED TO SELL PERP. STOP.")
        sys.stdout.flush()
        return False

    if hedge_symbol and hedge_size > 0:
        print("BUYBACK LINKED HEDGE:", hedge_symbol, hedge_size)
        sys.stdout.flush()
        place_market_order(hedge_symbol, "buy", hedge_size)

    state["batches"].pop()
    save_state(state)

    print("LAST BATCH CLOSED SUCCESSFULLY.")
    sys.stdout.flush()
    return True

def hedge_for_eligible_batches(state, live_price):
    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    eligible_size = 0.0
    for b in state["batches"]:
        if live_price >= float(b["buy_price"]):
            eligible_size += float(b["size"])

    if eligible_size <= 0:
        print("NO ELIGIBLE BATCHES FOR HEDGE SELL.")
        sys.stdout.flush()
        return

    # Prevent duplicate hedge
    option_positions = get_open_option_positions()
    for op in option_positions:
        if op["symbol"] == call_symbol and float(op["size"]) < 0:
            print("HEDGE ALREADY EXISTS ON EXCHANGE:", call_symbol, "size:", op["size"])
            sys.stdout.flush()
            return

    print("HEDGE SELL -> eligible lots:", eligible_size, "symbol:", call_symbol)
    sys.stdout.flush()

    resp = place_market_order(call_symbol, "sell", eligible_size)
    if resp.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL.")
        sys.stdout.flush()
        return

    # Save hedge in last batch for tracking
    state["batches"][-1]["hedge_symbol"] = call_symbol
    state["batches"][-1]["hedge_size"] = eligible_size
    save_state(state)

    print("HEDGE SOLD SUCCESSFULLY:", call_symbol, eligible_size)
    sys.stdout.flush()

def add_new_buy_batch(state, live_price):
    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    print("NEW BUY BATCH -> BUY PERP:", LOT_SIZE, "live_price:", live_price)
    sys.stdout.flush()

    resp = place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
    if resp.get("success") is not True:
        print("FAILED TO BUY PERP.")
        sys.stdout.flush()
        return

    # exact fill price from response
    fill_price = None
    try:
        fill_price = float(resp["result"]["average_fill_price"])
    except:
        fill_price = live_price

    print("SELL HEDGE CALL:", call_symbol, "SIZE:", LOT_SIZE)
    sys.stdout.flush()

    resp2 = place_market_order(call_symbol, "sell", LOT_SIZE)
    if resp2.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL.")
        sys.stdout.flush()
        return

    state["batches"].append({
        "buy_price": fill_price,
        "size": LOT_SIZE,
        "hedge_symbol": call_symbol,
        "hedge_size": LOT_SIZE
    })

    save_state(state)

    print("NEW BATCH ADDED -> buy_price:", fill_price, "size:", LOT_SIZE)
    sys.stdout.flush()

def daily_execute(state):
    print("DAILY EXECUTION STARTED...")
    sys.stdout.flush()

    # ALWAYS close today expiry call first
    close_today_expiry_hedges_if_any()

    live_price = get_live_price()
    pos_size = get_perp_position_size()

    print("DAILY EXECUTION -> LIVE:", live_price, "POS:", pos_size)
    sys.stdout.flush()

    if pos_size <= 0:
        print("NO POSITION FOUND. DAILY STRATEGY SKIPPED.")
        sys.stdout.flush()
        return

    if not state["batches"]:
        state["batches"] = [{
            "buy_price": live_price,
            "size": pos_size,
            "hedge_symbol": None,
            "hedge_size": 0.0
        }]
        save_state(state)

    # CASE-1 LOOP: if price >= last batch buy_price close it, continue loop
    while state["batches"]:
        live_price = get_live_price()
        last_price = float(state["batches"][-1]["buy_price"])

        if live_price >= last_price:
            print("PRICE ABOVE LAST BATCH -> closing batch. live:", live_price, "last:", last_price)
            sys.stdout.flush()

            ok = close_last_batch(state)
            if not ok:
                return

            time.sleep(1)
        else:
            break

    if not state["batches"]:
        print("ALL BATCHES CLOSED. END.")
        sys.stdout.flush()
        return

    # CASE-2 check down threshold
    last_batch_price = float(state["batches"][-1]["buy_price"])
    down_trigger = last_batch_price * (1 - THRESHOLD)

    print("CURRENT LAST BATCH PRICE:", last_batch_price)
    print("DOWN TRIGGER PRICE:", down_trigger)
    sys.stdout.flush()

    if live_price <= down_trigger:
        print("PRICE DOWN >= THRESHOLD -> NEW BUY + NEW HEDGE")
        sys.stdout.flush()
        add_new_buy_batch(state, live_price)
    else:
        print("PRICE NOT DOWN ENOUGH -> ONLY HEDGE SELL")
        sys.stdout.flush()
        hedge_for_eligible_batches(state, live_price)

    print("DAILY EXECUTION DONE.")
    sys.stdout.flush()

# =========================
# STARTUP
# =========================

state = load_state()

try:
    state = recover_state_from_exchange(state)
except Exception as e:
    print("RECOVERY ERROR:", str(e))
    traceback.print_exc()
    sys.stdout.flush()

# =========================
# MAIN LOOP (Never stop)
# =========================

try:
    while True:
        now_ist = datetime.now(IST)
        live_price = None
        pos_size = None

        try:
            live_price = get_live_price()
            pos_size = get_perp_position_size()
            print(now_ist.strftime("%Y-%m-%d %H:%M:%S"), "LIVE PRICE:", live_price, "| POS:", pos_size)
            sys.stdout.flush()
        except Exception as e:
            print("LIVE PRICE FETCH ERROR:", str(e))
            traceback.print_exc()
            sys.stdout.flush()

        # execute exactly at 3:30 PM IST once per day
        if now_ist.hour == 15 and now_ist.minute == 30:
            today_str = now_ist.strftime("%Y-%m-%d")

            if state.get("last_daily_run_date") != today_str:
                print("3:30 PM IST TRIGGERED -> Running daily strategy now...")
                sys.stdout.flush()

                try:
                    daily_execute(state)
                except Exception as e:
                    print("DAILY EXECUTION ERROR:", str(e))
                    traceback.print_exc()
                    sys.stdout.flush()

                state["last_daily_run_date"] = today_str
                save_state(state)

                print("DAILY RUN MARKED COMPLETE:", today_str)
                sys.stdout.flush()

                time.sleep(60)
            else:
                time.sleep(30)

        time.sleep(SLEEP_SECONDS)

except KeyboardInterrupt:
    print("BOT STOPPED MANUALLY.")
    sys.stdout.flush()
finally:
    release_lock()
