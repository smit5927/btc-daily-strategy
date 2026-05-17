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
# CONFIG (ENV SUPPORTED)
# =========================

BASE_URL = os.getenv("BASE_URL", "https://api.india.delta.exchange")

PERP_SYMBOL = os.getenv("PERP_SYMBOL", "ETHUSD")
UNDERLYING = os.getenv("UNDERLYING", "ETH")

STRIKE_STEP = int(os.getenv("STRIKE_STEP", "20"))
THRESHOLD = float(os.getenv("THRESHOLD", "0.005"))  # 0.5%

LOT_SIZE = float(os.getenv("LOT_SIZE", "1"))

SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "10"))

RUN_HOUR = int(os.getenv("RUN_HOUR", "15"))          # Default 3 PM
RUN_MINUTE = int(os.getenv("RUN_MINUTE", "30"))      # Default 30 min

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = os.getenv("STATE_FILE", "state.json")
LOCK_FILE = os.getenv("LOCK_FILE", "bot.lock")

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = "eth-daily-strategy-bot-FINAL-NO-MAXBATCH"

print("BOT FILE RUNNING...")
sys.stdout.flush()

print("BOT STARTED...")
print("BASE_URL:", BASE_URL)
print("PERP_SYMBOL:", PERP_SYMBOL)
print("UNDERLYING:", UNDERLYING)
print("LOT_SIZE:", LOT_SIZE)
print("STRIKE_STEP:", STRIKE_STEP)
print("THRESHOLD:", THRESHOLD)
print("RUN_TIME:", f"{RUN_HOUR}:{RUN_MINUTE} IST")
print("SLEEP_SECONDS:", SLEEP_SECONDS)
sys.stdout.flush()

if not DELTA_API_KEY or not DELTA_API_SECRET:
    raise Exception("Missing DELTA_API_KEY / DELTA_API_SECRET in environment!")

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

def default_state():
    return {
        "batches": [],
        "last_daily_run_date": None
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        d = default_state()
        d.update(data)
        return d

    except:
        return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =========================
# SAFE REQUEST
# =========================

def safe_json_response(r):
    try:
        return r.json()
    except Exception:
        return None


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
    data = safe_json_response(r)

    if data is None:
        raise Exception("PRIVATE GET JSON ERROR: " + r.text)

    return data


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
    data = safe_json_response(r)

    if data is None:
        raise Exception("PRIVATE POST JSON ERROR: " + r.text)

    return data


# =========================
# MARKET HELPERS
# =========================

def get_live_price():
    url = f"{BASE_URL}/v2/tickers/{PERP_SYMBOL}"
    r = requests.get(url, timeout=10)
    data = safe_json_response(r)

    if data is None:
        raise Exception("Ticker JSON error: " + r.text)

    if data.get("success") is not True:
        raise Exception("Ticker API failed: " + str(data))

    return float(data["result"]["close"])


def get_all_positions():
    data = private_get("/v2/positions/margined")

    if data.get("success") is not True:
        raise Exception("Positions API failed: " + str(data))

    return data.get("result", [])


def get_perp_position_size():
    positions = get_all_positions()

    for p in positions:
        if p.get("product_symbol") == PERP_SYMBOL:
            return float(p.get("size", 0))

    return 0.0


def get_option_positions():
    positions = get_all_positions()
    option_positions = []

    for p in positions:
        sym = p.get("product_symbol")
        size = float(p.get("size", 0))

        if sym and sym.startswith("C-ETH-") and size != 0:
            option_positions.append({"symbol": sym, "size": size})

    return option_positions


def get_last_buy_fill_price():
    data = private_get("/v2/fills", params={"page_size": 200})

    if data.get("success") is not True:
        raise Exception("Fills API failed: " + str(data))

    fills = data.get("result", [])

    for f in fills:
        if f.get("product_symbol") == PERP_SYMBOL and f.get("side") == "buy":
            return float(f.get("price"))

    return None


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


# =========================
# RECOVERY SYSTEM
# =========================

def safe_recover_batches(state_batches, exchange_pos_size, exchange_last_buy, exchange_option_positions):
    if exchange_pos_size <= 0:
        return []

    if state_batches and len(state_batches) > 0:
        return state_batches

    if exchange_last_buy is None:
        return []

    hedge_symbol = None
    hedge_size = 0.0

    for opt in exchange_option_positions:
        if opt["size"] < 0:
            hedge_symbol = opt["symbol"]
            hedge_size = abs(opt["size"])
            break

    return [{
        "buy_price": exchange_last_buy,
        "size": exchange_pos_size,
        "hedge_symbol": hedge_symbol,
        "hedge_size": hedge_size
    }]


# =========================
# STRATEGY CORE FUNCTIONS
# =========================

def close_today_expiry_calls():
    today_code = get_today_expiry_code()
    option_positions = get_option_positions()

    for opt in option_positions:
        sym = opt["symbol"]
        size = opt["size"]

        if today_code in sym and size < 0:
            print("TODAY EXPIRY CALL FOUND -> BUYBACK:", sym, abs(size))
            sys.stdout.flush()
            place_market_order(sym, "buy", abs(size))

    print("TODAY EXPIRY CHECK DONE.")
    sys.stdout.flush()


def close_last_batch(state):
    if not state["batches"]:
        return False

    last = state["batches"][-1]

    batch_size = float(last["size"])
    hedge_symbol = last.get("hedge_symbol")
    hedge_size = float(last.get("hedge_size", 0))

    exchange_pos = get_perp_position_size()

    if exchange_pos <= 0:
        print("NO EXCHANGE POSITION -> cannot close batch.")
        sys.stdout.flush()
        return False

    sell_size = min(batch_size, exchange_pos)

    if sell_size <= 0:
        print("SELL BLOCKED -> sell_size 0 (NO SHORT).")
        sys.stdout.flush()
        return False

    print("CLOSING LAST BATCH -> SELL PERP:", sell_size, "| BUY_PRICE:", last["buy_price"])
    sys.stdout.flush()

    resp = place_market_order(PERP_SYMBOL, "sell", sell_size)
    if resp.get("success") is not True:
        print("FAILED TO SELL PERP.")
        sys.stdout.flush()
        return False

    if hedge_symbol and hedge_size > 0:
        print("BUYBACK HEDGE:", hedge_symbol, hedge_size)
        sys.stdout.flush()

        resp2 = place_market_order(hedge_symbol, "buy", hedge_size)
        if resp2.get("success") is not True:
            print("WARNING: Hedge buyback failed:", hedge_symbol)
            sys.stdout.flush()

    state["batches"].pop()
    save_state(state)

    print("LAST BATCH CLOSED SUCCESSFULLY.")
    sys.stdout.flush()
    return True


def sell_hedge_for_all_open_batches(state, live_price):
    if not state["batches"]:
        print("NO BATCHES -> hedge sell skipped.")
        sys.stdout.flush()
        return

    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    total_open_size = 0.0
    for b in state["batches"]:
        total_open_size += float(b["size"])

    exchange_pos = get_perp_position_size()
    total_open_size = min(total_open_size, exchange_pos)

    if total_open_size <= 0:
        print("NO HEDGE SIZE AVAILABLE.")
        sys.stdout.flush()
        return

    print("SELLING HEDGE FOR ALL OPEN BATCHES:", call_symbol, "| SIZE:", total_open_size)
    sys.stdout.flush()

    resp = place_market_order(call_symbol, "sell", total_open_size)
    if resp.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL.")
        sys.stdout.flush()
        return

    state["batches"][-1]["hedge_symbol"] = call_symbol
    state["batches"][-1]["hedge_size"] = total_open_size
    save_state(state)

    print("HEDGE SOLD SUCCESSFULLY:", call_symbol, total_open_size)
    sys.stdout.flush()


def add_new_buy_batch(state, live_price):
    global LOT_SIZE

    LOT_SIZE = float(os.getenv("LOT_SIZE", str(LOT_SIZE)))

    expiry = get_tomorrow_expiry_code()
    strike = get_atm_strike(live_price)
    call_symbol = make_call_symbol(strike, expiry)

    print("NEW BUY BATCH -> BUY PERP:", LOT_SIZE, "AT PRICE:", live_price)
    sys.stdout.flush()

    resp = place_market_order(PERP_SYMBOL, "buy", LOT_SIZE)
    if resp.get("success") is not True:
        print("FAILED TO BUY PERP.")
        sys.stdout.flush()
        return False

    true_buy_price = get_last_buy_fill_price()
    if true_buy_price is None:
        true_buy_price = live_price

    print("SELL HEDGE CALL:", call_symbol, "SIZE:", LOT_SIZE)
    sys.stdout.flush()

    resp2 = place_market_order(call_symbol, "sell", LOT_SIZE)
    if resp2.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL.")
        sys.stdout.flush()
        return False

    state["batches"].append({
        "buy_price": true_buy_price,
        "size": LOT_SIZE,
        "hedge_symbol": call_symbol,
        "hedge_size": LOT_SIZE
    })

    save_state(state)

    print("NEW BATCH ADDED:", true_buy_price, LOT_SIZE)
    sys.stdout.flush()
    return True


def ensure_reentry_if_all_closed(state, live_price):
    pos_size = get_perp_position_size()
    option_positions = get_option_positions()

    if pos_size > 0:
        return

    for opt in option_positions:
        if opt["size"] < 0:
            print("RE-ENTRY BLOCKED -> Hedge option still open:", opt)
            sys.stdout.flush()
            return

    print("RE-ENTRY -> NO POSITION FOUND. ENTERING AGAIN...")
    sys.stdout.flush()

    add_new_buy_batch(state, live_price)


# =========================
# DAILY EXECUTION (FULL LOGIC)
# =========================

def daily_execute(state):
    global LOT_SIZE

    LOT_SIZE = float(os.getenv("LOT_SIZE", str(LOT_SIZE)))

    print("STEP-0: TODAY EXPIRY CLOSE CHECK START")
    sys.stdout.flush()
    close_today_expiry_calls()

    live_price = get_live_price()
    pos_size = get_perp_position_size()

    print("DAILY EXECUTION START -> LIVE:", live_price, "POS:", pos_size)
    sys.stdout.flush()

    if pos_size <= 0:
        print("NO PERP POSITION FOUND -> RE-ENTRY LOGIC.")
        sys.stdout.flush()
        ensure_reentry_if_all_closed(state, live_price)
        return

    # If state empty -> initialize from exchange
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
            print("STATE INITIALIZED FROM EXCHANGE BUY:", last_buy)
            sys.stdout.flush()

    if not state["batches"]:
        print("STATE STILL EMPTY -> SKIPPING.")
        sys.stdout.flush()
        return

    # =========================
    # STEP-1: CLOSE MULTIPLE BATCHES IF PRICE GAP UP
    # =========================
    while state["batches"]:
        last_buy_price = float(state["batches"][-1]["buy_price"])
        up_trigger = last_buy_price * (1 + THRESHOLD)

        if live_price >= up_trigger:
            print("PRICE UP >= 0.5% FROM LAST BUY -> CLOSE LAST BATCH")
            sys.stdout.flush()

            closed = close_last_batch(state)
            if not closed:
                break

            live_price = get_live_price()
            continue

        break

    # If all batches closed -> reentry
    if not state["batches"]:
        print("ALL BATCHES CLOSED -> RE-ENTRY REQUIRED")
        sys.stdout.flush()
        ensure_reentry_if_all_closed(state, live_price)
        return

    # =========================
    # STEP-2: NOW DECIDE BUY OR ONLY HEDGE
    # =========================
    last_batch_price = float(state["batches"][-1]["buy_price"])
    down_trigger = last_batch_price * (1 - THRESHOLD)

    print("CURRENT LAST BATCH PRICE:", last_batch_price)
    print("DOWN TRIGGER PRICE:", down_trigger)
    sys.stdout.flush()

    if live_price <= down_trigger:
        print("PRICE DOWN >= 0.5% -> NEW BUY + NEW HEDGE")
        sys.stdout.flush()
        add_new_buy_batch(state, live_price)

    else:
        print("PRICE NOT DOWN ENOUGH -> ONLY SELL HEDGE FOR ALL OPEN LOTS")
        sys.stdout.flush()
        sell_hedge_for_all_open_batches(state, live_price)

    ensure_reentry_if_all_closed(state, live_price)

    print("DAILY EXECUTION DONE.")
    sys.stdout.flush()


# =========================
# STARTUP RECOVERY
# =========================

state = load_state()

print("RECOVERY STARTED...")
sys.stdout.flush()

try:
    pos_size = get_perp_position_size()
    last_buy = get_last_buy_fill_price()
    option_positions = get_option_positions()

    print("EXCHANGE PERP POSITION SIZE:", pos_size)
    print("EXCHANGE LAST BUY FILL:", last_buy)
    print("EXCHANGE OPTION POSITIONS:", option_positions)
    sys.stdout.flush()

    state["batches"] = safe_recover_batches(
        state.get("batches", []),
        pos_size,
        last_buy,
        option_positions
    )

    save_state(state)

    print("RECOVERY DONE. BATCHES:", state["batches"])
    sys.stdout.flush()

except Exception as e:
    print("RECOVERY ERROR:", str(e))
    traceback.print_exc()
    sys.stdout.flush()


# =========================
# MAIN LOOP
# =========================

try:
    while True:
        try:
            now_ist = datetime.now(IST)

            live_price = get_live_price()
            pos_size = get_perp_position_size()

            print(now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                  "LIVE PRICE:", live_price,
                  "| POS:", pos_size,
                  "| BATCHES:", len(state.get("batches", [])))
            sys.stdout.flush()

            if now_ist.hour == RUN_HOUR and now_ist.minute == RUN_MINUTE:
                today_str = now_ist.strftime("%Y-%m-%d")

                if state.get("last_daily_run_date") != today_str:
                    print("DAILY TIME TRIGGERED -> Running strategy now...")
                    sys.stdout.flush()

                    daily_execute(state)

                    state["last_daily_run_date"] = today_str
                    save_state(state)

                    print("DAILY RUN MARKED COMPLETE:", today_str)
                    sys.stdout.flush()

                else:
                    print("Already executed today. Waiting next day...")
                    sys.stdout.flush()

                time.sleep(60)

            else:
                time.sleep(SLEEP_SECONDS)

        except Exception as e:
            print("RUNTIME ERROR:", str(e))
            traceback.print_exc()
            sys.stdout.flush()
            time.sleep(10)

except KeyboardInterrupt:
    print("BOT STOPPED MANUALLY.")
    sys.stdout.flush()

finally:
    release_lock()
