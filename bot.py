# HUSSAIN BTCUSDT 9-21 EMA TELEGRAM BOT
# Educational alert bot only. It does NOT place orders.
# Requirements: Pydroid 3 + requests
import requests, time, json, os, traceback
from datetime import datetime, timezone

SYMBOL = "BTCUSDT"
EXEC_TF = "5m"
FAST_EMA = 9
SLOW_EMA = 21
POLL_SECONDS = 12
STATE_FILE = "hussnain_bot_state.json"
CONFIG_FILE = "hussnain_bot_config.json"
BASE = "https://api.binance.com"
KLINE_LIMIT = 240

# Strategy risk filters (implementation settings; adjust only after paper testing)
MAX_SL_ATR = 2.8          # skip unusually wide structural stop
MAX_ENTRY_DISTANCE_ATR = 2.5
SPIKE_ATR = 2.8           # avoid sudden oversized/news-like candles
PULLBACK_MAX_AGE = 18
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
MIN_RR = 2.0

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secret.")
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

def send(msg):
    try:
        r = requests.post(TG_URL, data={"chat_id": CHAT_ID, "text": msg}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            print("Telegram error:", data)
    except Exception as e:
        print("Telegram send error:", e)

def get_klines(interval, limit=KLINE_LIMIT):
    r = requests.get(BASE + "/api/v3/klines",
        params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    raw = r.json()
    # Ignore the currently forming candle; only closed candles are used.
    raw = raw[:-1]
    return [{
        "t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
        "l": float(x[3]), "c": float(x[4]), "v": float(x[5])
    } for x in raw]

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    out = [None] * len(values)
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1-k)
        out[i] = prev
    return out

def atr(candles, period=14):
    trs = []
    for i, x in enumerate(candles):
        if i == 0:
            tr = x["h"] - x["l"]
        else:
            pc = candles[i-1]["c"]
            tr = max(x["h"]-x["l"], abs(x["h"]-pc), abs(x["l"]-pc))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs)/max(1,len(trs))
    return sum(trs[-period:])/period

def pivots(candles, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    highs, lows = [], []
    for i in range(left, len(candles)-right):
        h = candles[i]["h"]; l = candles[i]["l"]
        if all(h > candles[j]["h"] for j in range(i-left,i)) and all(h >= candles[j]["h"] for j in range(i+1,i+right+1)):
            highs.append((i,h))
        if all(l < candles[j]["l"] for j in range(i-left,i)) and all(l <= candles[j]["l"] for j in range(i+1,i+right+1)):
            lows.append((i,l))
    return highs, lows

def market_structure(candles):
    ph, pl = pivots(candles)
    if len(ph) < 2 or len(pl) < 2:
        return "SIDEWAYS"
    h1,h2 = ph[-2][1],ph[-1][1]
    l1,l2 = pl[-2][1],pl[-1][1]
    if h2 > h1 and l2 > l1:
        return "UP"
    if h2 < h1 and l2 < l1:
        return "DOWN"
    return "SIDEWAYS"

def top_down_bias(daily, four_hour):
    d = market_structure(daily)
    h = market_structure(four_hour)
    # Lesson 5: use 1D first; if unclear, consult 4H.
    if d in ("UP","DOWN"):
        if h in ("UP","DOWN") and h != d:
            return "NONE", f"1D {d}, 4H {h} conflict"
        return d, f"Bias from 1D ({d}); 4H={h}"
    if h in ("UP","DOWN"):
        return h, f"1D unclear; 4H confirms {h}"
    return "NONE", "1D and 4H trend unclear/sideways"

def build_zones(candles, atr_value):
    ph, pl = pivots(candles)
    vals = [p for _,p in ph[-18:]] + [p for _,p in pl[-18:]]
    if not vals:
        return []
    vals.sort()
    tolerance = max(atr_value * 0.35, 1.0)
    groups=[]
    for p in vals:
        if not groups or p - groups[-1][-1] > tolerance:
            groups.append([p])
        else:
            groups[-1].append(p)
    zones=[]
    for g in groups:
        if len(g) >= 2:
            zones.append((min(g), max(g)))
    return zones

def zone_filter(entry, target, zones):
    lo, hi = sorted((entry,target))
    for zlo,zhi in zones:
        # avoid entries inside a zone and target path blocked by a zone
        if zlo <= entry <= zhi:
            return False, f"Entry {entry:.2f} is inside HTF zone {zlo:.2f}-{zhi:.2f}"
        if max(lo,zlo) < min(hi,zhi) or (lo <= zlo <= hi) or (lo <= zhi <= hi):
            return False, f"HTF zone {zlo:.2f}-{zhi:.2f} blocks path to TP2"
    return True, ""

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE,"r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_candle":0,"pending":None,"trade":None,"bias":None,"last_signal":None}

def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp,"w") as f:
        json.dump(s,f)
    os.replace(tmp,STATE_FILE)

def fmt_time(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def make_pending(direction, candle, candles, source="CROSS"):
    # Locate the most recent confirmed structural swing before the crossover.
    ph,pl = pivots(candles[:-1])
    if direction == "BUY":
        candidate = pl[-1][1] if pl else None
    else:
        candidate = ph[-1][1] if ph else None
    return {
        "direction":direction, "created":candle["t"], "age":0,
        "source":source, "swing":candidate,
        "pullback_high":None, "pullback_low":None,
        "pullback_started":False, "pullback_age":0
    }

def alert_entry(direction, entry, sl, tp1, tp2, note, candle):
    msg = (
        f"📣 BTCUSDT {direction} SIGNAL (5M)\n"
        f"Entry / Trigger: {entry:.2f}\n"
        f"Structural SL: {sl:.2f}\n"
        f"TP1 (1R): {tp1:.2f}\n"
        f"TP2 (2R): {tp2:.2f}\n"
        f"Setup: {note}\n"
        f"Candle close: {fmt_time(candle['t'])}\n"
        f"⚠️ Alert only — order خود لگائیں، bot خود trade نہیں کرتا۔"
    )
    send(msg)

def enter_trade(state, direction, entry, sl, note, candle, zones, atr_value):
    risk = abs(entry-sl)
    if risk <= 0 or risk > MAX_SL_ATR*atr_value:
        send(f"⚪ {direction} setup skipped: structural SL too wide ({risk:.2f}; ATR {atr_value:.2f}).")
        state["pending"] = None
        return
    if risk/entry > 0.02:
        send(f"⚪ {direction} setup skipped: SL distance exceeds 2% of entry.")
        state["pending"] = None
        return
    tp1 = entry + risk if direction=="BUY" else entry-risk
    tp2 = entry + MIN_RR*risk if direction=="BUY" else entry-MIN_RR*risk
    ok, reason = zone_filter(entry,tp2,zones)
    if not ok:
        send(f"⚪ {direction} setup skipped by top-down zones: {reason}")
        state["pending"] = None
        return
    state["trade"] = {
        "direction":direction,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
        "tp1_hit":False,"tp1_candle":0,"created":candle["t"],"note":note
    }
    state["pending"] = None
    alert_entry(direction,entry,sl,tp1,tp2,note,candle)

def manage_trade(state, candle):
    tr = state.get("trade")
    if not tr: return
    d=tr["direction"]; entry=tr["entry"]; sl=tr["sl"]; tp1=tr["tp1"]; tp2=tr["tp2"]
    # Conservative same-candle rule: if SL and target both lie in candle range, count SL first.
    sl_hit = candle["l"] <= sl if d=="BUY" else candle["h"] >= sl
    tp1_hit = candle["h"] >= tp1 if d=="BUY" else candle["l"] <= tp1
    tp2_hit = candle["h"] >= tp2 if d=="BUY" else candle["l"] <= tp2
    if sl_hit:
        send(f"🛑 BTCUSDT {d} STOP LOSS HIT\nEntry: {entry:.2f}\nSL: {sl:.2f}\nTrade closed.")
        state["trade"]=None
        return
    if tp1_hit and not tr["tp1_hit"]:
        tr["tp1_hit"]=True; tr["tp1_candle"]=candle["t"]
        send(f"✅ BTCUSDT {d} TP1 HIT (1R)\nEntry: {entry:.2f}\nTP1: {tp1:.2f}\nTP2 remains active. If price later returns to entry, treat it as breakeven.")
    if tp2_hit:
        send(f"🎯 BTCUSDT {d} TP2 HIT (2R)\nEntry: {entry:.2f}\nTP2: {tp2:.2f}\nTrade closed.")
        state["trade"]=None
        return
    if tr["tp1_hit"] and candle["t"] > tr["tp1_candle"]:
        returned = candle["l"] <= entry if d=="BUY" else candle["h"] >= entry
        if returned:
            send(f"↩️ BTCUSDT {d} BREAKEVEN EXIT\nTP1 was hit; price returned to entry {entry:.2f}. Trade closed.")
            state["trade"]=None
            return
    state["trade"]=tr

def update_pending(state, candles, candle, ema9, ema21, zones, atr_value, bias):
    p=state.get("pending")
    if not p: return
    p["age"] += 1
    d=p["direction"]
    if p["age"] > PULLBACK_MAX_AGE:
        state["pending"]=None
        return
    # If higher timeframe bias changes, discard setup.
    if bias != d:
        state["pending"]=None
        return
    # First prefer a valid nearby prior swing break (primary entry).
    swing=p.get("swing")
    if swing is not None and abs(swing-candle["c"]) <= MAX_ENTRY_DISTANCE_ATR*atr_value:
        broken = candle["c"] > swing if d=="BUY" else candle["c"] < swing
        if broken:
            # Structural stop sits beyond the reversal swing used for this setup.
            buffer = 0.10 * atr_value
            sl = swing - buffer if d=="BUY" else swing + buffer
            enter_trade(state,d,candle["c"],sl,"Previous reversal-swing breakout",candle,zones,atr_value)
            return
    # Pullback: opposite-colour candle reaches either 9 or 21 EMA.
    tol=0.18*atr_value
    if d=="BUY":
        opposite = candle["c"] < candle["o"]
        touched = candle["l"] <= max(ema9,ema21)+tol
    else:
        opposite = candle["c"] > candle["o"]
        touched = candle["h"] >= min(ema9,ema21)-tol

    if not p["pullback_started"]:
        if opposite and touched:
            p["pullback_started"]=True
            p["pullback_high"]=candle["h"]
            p["pullback_low"]=candle["l"]
            p["pullback_age"]=1
    else:
        # Test breakout against the already-marked pullback level BEFORE
        # adding the current candle to the pullback range.
        ph=p["pullback_high"]; pl=p["pullback_low"]
        trigger = ph if d=="BUY" else pl
        breakout = candle["c"] > trigger if d=="BUY" else candle["c"] < trigger
        ema_confirm = candle["c"] > max(ema9,ema21) if d=="BUY" else candle["c"] < min(ema9,ema21)
        if breakout and ema_confirm:
            sl = pl - 0.10*atr_value if d=="BUY" else ph + 0.10*atr_value
            enter_trade(state,d,candle["c"],sl,"EMA pullback breakout (closed candle)",candle,zones,atr_value)
            return
        p["pullback_age"] += 1
        p["pullback_high"] = max(ph,candle["h"])
        p["pullback_low"] = min(pl,candle["l"])
    state["pending"]=p

def process():
    state=load_state()
    # Load all timeframes used in the top-down method.
    d1=get_klines("1d",180)
    h4=get_klines("4h",180)
    h1=get_klines("1h",180)
    m15=get_klines("15m",180)
    m5=get_klines("5m",KLINE_LIMIT)
    if len(m5)<60: return
    candle=m5[-1]
    if candle["t"] <= state.get("last_candle",0): return

    bias,bias_note=top_down_bias(d1,h4)
    a=atr(m5[:-1])
    zones=build_zones(h1,atr(h1))
    if not zones:
        zones=build_zones(m15,atr(m15))

    closes=[x["c"] for x in m5]
    e9=ema(closes,FAST_EMA); e21=ema(closes,SLOW_EMA)
    i=len(m5)-1
    if e9[i] is None or e21[i] is None or e9[i-1] is None or e21[i-1] is None:
        state["last_candle"]=candle["t"]; save_state(state); return

    # Persist candle cursor once the candle is accepted; process each closed candle once.
    state["last_candle"]=candle["t"]
    if state.get("trade"):
        manage_trade(state,candle)
    elif bias in ("UP","DOWN"):
        direction="BUY" if bias=="UP" else "SELL"
        update_pending(state,m5,candle,e9[i],e21[i],zones,a,direction)
        if not state.get("trade") and not state.get("pending"):
            # New crossover starts a fresh primary setup.
            cross_up=e9[i-1] <= e21[i-1] and e9[i] > e21[i]
            cross_dn=e9[i-1] >= e21[i-1] and e9[i] < e21[i]
            if (direction=="BUY" and cross_up) or (direction=="SELL" and cross_dn):
                # Skip extreme spike / Burj-Khalifa style candle.
                if candle["h"]-candle["l"] <= SPIKE_ATR*a:
                    state["pending"]=make_pending(direction,candle,m5[:-1],"CROSS")
                    send(f"🔎 BTCUSDT {direction} 9/21 EMA crossover confirmed on 5M.\n{bias_note}\nWaiting for valid prior-swing or EMA-pullback breakout; no entry yet.")
            # Continuation/re-entry: after a trend is already established, one opposite-colour EMA touch
            # is treated as a single-candle pullback; breakout is still required.
            elif not state.get("pending"):
                opposite=(candle["c"]<candle["o"]) if direction=="BUY" else (candle["c"]>candle["o"])
                touched=(candle["l"]<=max(e9[i],e21[i])+0.18*a) if direction=="BUY" else (candle["h"]>=min(e9[i],e21[i])-0.18*a)
                if opposite and touched:
                    state["pending"]={
                        "direction":direction,"created":candle["t"],"age":0,"source":"REENTRY",
                        "swing":None,"pullback_high":candle["h"],"pullback_low":candle["l"],
                        "pullback_started":True,"pullback_age":1
                    }
        state["bias"]=direction
    else:
        state["pending"]=None
        state["bias"]=None
    save_state(state)

def main():
    send("🤖 HUSSAIN BTCUSDT 9-21 EMA BOT STARTED\nExecution: 5 Minutes\nTop-down: 1D trend → 4H fallback → 1H zones (15M fallback) → 5M entry\nSignals: BUY/SELL, structural SL, TP1=1R, TP2=2R, TP/SL/BE alerts\nClosed candles only. Automatic orders OFF.")
    print("Bot running. Keep Pydroid open and phone connected to power.")
    while True:
        try:
            process()
        except KeyboardInterrupt:
            send("⏹️ HUSSAIN bot stopped.")
            break
        except Exception as e:
            print("Error:",e)
            traceback.print_exc()
            time.sleep(5)
        time.sleep(POLL_SECONDS)

if __name__=="__main__":
    main()
