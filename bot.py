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

STRIKE_STEP = int(os.getenv("STRIKE_STEP", "20"))     # ETH strike step = 20
THRESHOLD = float(os.getenv("THRESHOLD", "0.005"))    # 0.5% default
LOT_SIZE = float(os.getenv("LOT_SIZE", "1"))          # set from Render env

DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET")

STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"

SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "10"))

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = "eth-daily-strategy-bot"

# =========================
# PRINT STARTUP
# =========================

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
# LOCK SYSTEM (NO DUPLICATE RUN)
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
# SAFE REQUEST HELPERS (NO CRASH)
# =========================

def safe_json_response(r):
    try:
        return r.json()
    except Exception:
        return None


def safe_request(method, url, headers=None, params=None, data=None, timeout=20, retries=5):
    for i in range(retries):
        try:
            r = requests.request(method, url, headers=headers, params=params, data=data, timeout=timeout)

            # sometimes server returns empty response
            if not r.text or len(r.text.strip()) == 0:
                print("WARNING: Empty API response, retrying...")
                sys.stdout.flush()
                time.sleep(1)
                continue

            return r

        except Exception as e:
            print("REQUEST ERROR:", str(e), "| retry:", i + 1)
            sys.stdout.flush()
            time.sleep(2)

    raise Exception("API request failed after retries: " + url)

# =========================
# STATE MANAGEMENT
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"batches": [], "last_daily_run_date": None}

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"batches": [], "last_daily_run_date": None}


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

    r = safe_request("GET", url, headers=headers, timeout=20)
    data = safe_json_response(r)

    if data is None:
        raise Exception("GET JSON decode failed for: " + endpoint)

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

    r = safe_request("POST", url, headers=headers, data=body, timeout=20)
    data = safe_json_response(r)

    if data is None:
        raise Exception("POST JSON decode failed for: " + endpoint)

    return data


# =========================
# MARKET HELPERS
# =========================

def get_live_price():
    url = f"{BASE_URL}/v2/tickers/{PERP_SYMBOL}"
    r = safe_request("GET", url, timeout=10)
    data = safe_json_response(r)

    if not data or data.get("success") is not True:
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


def get_open_option_positions():
    positions = get_all_positions()
    option_positions = []

    for p in positions:
        sym = p.get("product_symbol", "")
        size = float(p.get("size", 0))

        if sym.startswith("C-ETH-") or sym.startswith("P-ETH-"):
            if abs(size) > 0:
                option_positions.append({
                    "symbol": sym,
                    "size": size
                })

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

def get_tomorrow_expiry_code():
    now_ist = datetime.now(IST)
    tomorrow = now_ist + timedelta(days=1)
    return tomorrow.strftime("%d%m%y")


def get_atm_strike(price: float):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def make_call_symbol(strike: int, expiry_code: str):
    return f"C-{UNDERLYING}-{strike}-{expiry_code}"


# =========================
# RECOVERY SYSTEM (STATE + EXCHANGE VERIFY)
# =========================

def safe_recover_batches(state_batches, exchange_pos_size, exchange_last_buy, exchange_option_positions):
    """
    FINAL RECOVERY RULES:

    1) If no perp position -> no batches
    2) If state already has batches -> keep but verify sizes
    3) If state missing -> rebuild from exchange last buy fill
    4) Detect hedge from exchange open option positions and map into batch
    """

    if exchange_pos_size <= 0:
        return []

    # if state already has batches, use it
    if state_batches and len(state_batches) > 0:
        # Verify total size
        total_state_size = sum(float(b.get("size", 0)) for b in state_batches)

        if abs(total_state_size - exchange_pos_size) > 0.0001:
            print("WARNING: State total size mismatch with exchange.")
            print("STATE SIZE:", total_state_size, "EXCHANGE SIZE:", exchange_pos_size)
            print("Auto-fixing by scaling last batch size...")

            diff = exchange_pos_size - total_state_size
            state_batches[-1]["size"] = float(state_batches[-1]["size"]) + diff

        # If hedge missing, try to attach exchange hedge
        if exchange_option_positions:
            # find biggest sell call
            best_call = None
            for op in exchange_option_positions:
                if op["symbol"].startswith("C-ETH-") and op["size"] < 0:
                    if best_call is None or abs(op["size"]) > abs(best_call["size"]):
                        best_call = op

            if best_call:
                print("RECOVERY FOUND EXCHANGE CALL SELL:", best_call)

                # attach hedge to last batch
                state_batches[-1]["hedge_symbol"] = best_call["symbol"]
                state_batches[-1]["hedge_size"] = abs(best_call["size"])

        return state_batches

    # if state empty -> rebuild from exchange last buy fill
    if exchange_last_buy is None:
        return []

    rebuilt = [{
        "buy_price": exchange_last_buy,
        "size": exchange_pos_size,
        "hedge_symbol": None,
        "hedge_size": 0.0
    }]

    # attach hedge from exchange if exists
    if exchange_option_positions:
        best_call = None
        for op in exchange_option_positions:
            if op["symbol"].startswith("C-ETH-") and op["size"] < 0:
                if best_call is None or abs(op["size"]) > abs(best_call["size"]):
                    best_call = op

        if best_call:
            rebuilt[-1]["hedge_symbol"] = best_call["symbol"]
            rebuilt[-1]["hedge_size"] = abs(best_call["size"])

    return rebuilt


# =========================
# STRATEGY FUNCTIONS
# =========================

def close_last_batch(state, live_price):
    if not state["batches"]:
        return

    last = state["batches"][-1]

    size = float(last["size"])
    hedge_symbol = last.get("hedge_symbol")
    hedge_size = float(last.get("hedge_size", 0))

    print("CLOSING LAST BATCH -> buy_price:", last["buy_price"], "size:", size)
    sys.stdout.flush()

    # SELL PERP
    resp = place_market_order(PERP_SYMBOL, "sell", size)
    if resp.get("success") is not True:
        print("FAILED TO SELL PERP. STOP.")
        sys.stdout.flush()
        return

    # BUYBACK HEDGE (if exists)
    if hedge_symbol and hedge_size > 0:
        print("BUYBACK HEDGE:", hedge_symbol, hedge_size)
        sys.stdout.flush()

        resp2 = place_market_order(hedge_symbol, "buy", hedge_size)
        if resp2.get("success") is not True:
            print("FAILED TO BUYBACK HEDGE. MANUAL CHECK REQUIRED.")
            sys.stdout.flush()

    state["batches"].pop()
    save_state(state)

    print("LAST BATCH CLOSED SUCCESSFULLY.")
    sys.stdout.flush()


def hedge_for_eligible_batches(state, live_price):
    """
    RULE:
    Sell hedge only for those batches whose buy_price <= live_price
    (means they are in profit / above buy)
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
        sys.stdout.flush()
        return

    # IMPORTANT: check exchange if already have this call sell
    open_opts = get_open_option_positions()
    already_sold = 0.0
    for op in open_opts:
        if op["symbol"] == call_symbol and op["size"] < 0:
            already_sold += abs(op["size"])

    if already_sold >= eligible_size - 0.0001:
        print("HEDGE ALREADY SOLD ON EXCHANGE -> SKIPPING")
        print("SYMBOL:", call_symbol, "EXCHANGE SOLD:", already_sold, "NEEDED:", eligible_size)
        sys.stdout.flush()
        return

    need_to_sell = eligible_size - already_sold

    if need_to_sell <= 0:
        print("NO NEW HEDGE SELL REQUIRED.")
        sys.stdout.flush()
        return

    print("HEDGE SELL -> eligible lots:", eligible_size, "| already_sold:", already_sold, "| new_sell:", need_to_sell)
    print("CALL SYMBOL:", call_symbol)
    sys.stdout.flush()

    resp = place_market_order(call_symbol, "sell", need_to_sell)
    if resp.get("success") is not True:
        print("FAILED TO SELL HEDGE CALL.")
        sys.stdout.flush()
        return

    # store hedge info in last batch
    state["batches"][-1]["hedge_symbol"] = call_symbol
    state["batches"][-1]["hedge_size"] = eligible_size
    save_state(state)

    print("HEDGE SOLD SUCCESSFULLY:", call_symbol, eligible_size)
    sys.stdout.flush()


def add_new_buy_batch(state, live_price):
    """
    Buy PERP new LOT_SIZE
    Immediately sell ATM call tomorrow expiry same LOT_SIZE
    """

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

    true_buy_price = get_last_buy_fill_price()
    if true_buy_price is None:
        true_buy_price = live_price

    # check if hedge already sold on exchange
    open_opts = get_open_option_positions()
    already_sold = 0.0
    for op in open_opts:
        if op["symbol"] == call_symbol and op["size"] < 0:
            already_sold += abs(op["size"])

    if already_sold >= LOT_SIZE - 0.0001:
        print("HEDGE CALL ALREADY SOLD. SKIPPING NEW HEDGE SELL.")
        sys.stdout.flush()
    else:
        need_to_sell = LOT_SIZE - already_sold
        if need_to_sell > 0:
            print("SELL HEDGE CALL:", call_symbol, "SIZE:", need_to_sell)
            sys.stdout.flush()

            resp2 = place_market_order(call_symbol, "sell", need_to_sell)
            if resp2.get("success") is not True:
                print("FAILED TO SELL HEDGE CALL.")
                sys.stdout.flush()
                return

    state["batches"].append({
        "buy_price": true_buy_price,
        "size": LOT_SIZE,
        "hedge_symbol": call_symbol,
        "hedge_size": LOT_SIZE
    })

    save_state(state)

    print("NEW BATCH ADDED -> buy_price:", true_buy_price, "size:", LOT_SIZE)
    sys.stdout.flush()


def daily_execute(state):
    """
    FULL STRATEGY:

    1) Close last batch repeatedly if live_price >= last_batch_price
       (sell perp + buyback hedge)
       loop continues until condition fails.

    2) After loop, if no batches left -> stop.

    3) Compare live_price vs new last_batch_price:
       If live_price <= last_batch_price*(1-THRESHOLD)
            -> new buy batch + hedge
       Else
            -> only hedge sell for eligible batches
    """

    live_price = get_live_price()
    pos_size = get_perp_position_size()

    print("DAILY EXECUTION START -> LIVE:", live_price, "POS:", pos_size)
    sys.stdout.flush()

    if pos_size <= 0:
        print("NO POSITION FOUND. DAILY STRATEGY SKIPPED.")
        sys.stdout.flush()
        return

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
            sys.stdout.flush()

    # LOOP: close batches if price >= last batch buy price
    while state["batches"]:
        last_price = float(state["batches"][-1]["buy_price"])

        if live_price >= last_price:
            print("PRICE ABOVE LAST BATCH -> closing batch...")
            sys.stdout.flush()

            close_last_batch(state, live_price)

            # update price after closing
            live_price = get_live_price()

        else:
            break

    if not state["batches"]:
        print("ALL BATCHES CLOSED. END.")
        sys.stdout.flush()
        return

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
        print("PRICE NOT DOWN ENOUGH -> ONLY HEDGE SELL FOR ELIGIBLE")
        sys.stdout.flush()
        hedge_for_eligible_batches(state, live_price)

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
    option_positions = get_open_option_positions()

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
# MAIN LOOP (ALWAYS RUNNING)
# =========================

try:
    while True:
        try:
            now_ist = datetime.now(IST)
            live_price = get_live_price()
            pos_size = get_perp_position_size()

            print(now_ist.strftime("%Y-%m-%d %H:%M:%S"), "LIVE PRICE:", live_price, "| POS:", pos_size)
            sys.stdout.flush()

            # execute exactly at 3:30 PM IST (one time daily)
            if now_ist.hour == 15 and now_ist.minute == 30:
                today_str = now_ist.strftime("%Y-%m-%d")

                if state.get("last_daily_run_date") != today_str:
                    print("3:30 PM IST TRIGGERED -> Running daily strategy now...")
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
            print("MAIN LOOP ERROR:", str(e))
            traceback.print_exc()
            sys.stdout.flush()
            time.sleep(10)

except KeyboardInterrupt:
    print("BOT STOPPED MANUALLY.")
    sys.stdout.flush()

finally:
    release_lock()
