"""
Telegram signal boti - GitHub Actions uchun (v2: kechikishga chidamli).

Har ishga tushganda:
  1) narxni Yahoo Finance dan oladi
  2) oxirgi LOOKBACK ta yopilgan shamni tekshiradi (ishga tushirish kechiksa ham signal o'tib ketmaydi)
  3) signal topilsa va narx hali kirish uchun yaroqli bo'lsa Telegramga yuboradi
  4) ochiq signal TP yoki SL ga yetgan bo'lsa xabar beradi

Maxfiy ma'lumotlar (GitHub Secrets):  TG_TOKEN, TG_CHAT_ID
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ===================== SOZLAMALAR =====================
INTERVAL = "15m"
CANDLE_SEC = 15 * 60
LOOKBACK = 6            # oxirgi nechta yopilgan shamdan signal qidiriladi (6 = ~90 daqiqa)
MAX_MOVE_FRAC = 0.5     # narx signal shamidan SL masofasining shuncha ulushidan ko'p yurgan bo'lsa, signal yuborilmaydi
# tp / sl - NARX masofasi. XAUUSD: tp 25 = $25 harakat (0.02 lotda ~ $50 foyda), sl 12.5 (~ $25 zarar)
SYMBOLS = {
    "XAUUSD": {"yahoo": "GC=F",     "tp": 25.0,   "sl": 12.5,   "digits": 2},
}
STATE_FILE = "state.json"
# ======================================================

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]


def send(text):
    print(f"[{datetime.utcnow():%H:%M:%S} UTC] {text.replace(chr(10), ' | ')}")
    for cid in CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data), timeout=30)
        except Exception as e:
            print("Telegram xatosi:", cid, e)


def get_candles(yahoo_symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(yahoo_symbol)}?interval={INTERVAL}&range=10d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for t, c, h, l in zip(res["timestamp"], q["close"], q["high"], q["low"]):
        if c is not None and h is not None and l is not None:
            out.append({"t": t, "c": c, "h": h, "l": l})
    return out


def ema(vals, span):
    a = 2 / (span + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def rsi(vals, period=14):
    a = 1 / period
    out = [50.0]
    ag = al = None
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if ag is None:
            ag, al = g, l
        else:
            ag = a * g + (1 - a) * ag
            al = a * l + (1 - a) * al
        if al == 0:
            out.append(100.0 if ag > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + ag / al))
    return out


def find_signals(closes, lookback):
    """Oxirgi `lookback` ta yopilgan shamning har biri uchun signalni tekshiradi.
    [(indeks, 'BUY'/'SELL', info), ...] qaytaradi (eskidan yangiga)."""
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    n = len(closes)
    found = []
    for i in range(max(1, n - lookback), n):
        up = e20[i] > e50[i] > e200[i]
        down = e20[i] < e50[i] < e200[i]
        cross_up = r[i - 1] < 50 <= r[i]
        cross_down = r[i - 1] > 50 >= r[i]
        info = f"Trend: {'UP' if up else 'DOWN' if down else 'FLAT'} | RSI: {r[i]:.1f}"
        if up and cross_up:
            found.append((i, "BUY", info))
        elif down and cross_down:
            found.append((i, "SELL", info))
    return found, r[-1], (e20[-1] > e50[-1] > e200[-1]), (e20[-1] < e50[-1] < e200[-1])


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("open", {})
    s.setdefault("last_sig", {})
    return s


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def check_open(s, candles):
    """Signal ochilgandan keyingi shamlarning high/low ini tekshiradi.
    Bir shamda ikkalasi ham urilsa, konservativ: SL deb hisoblanadi."""
    for c in candles:
        if c["t"] <= s["ts"]:
            continue
        if s["side"] == "BUY":
            hit_sl, hit_tp = c["l"] <= s["sl"], c["h"] >= s["tp"]
        else:
            hit_sl, hit_tp = c["h"] >= s["sl"], c["l"] <= s["tp"]
        if hit_sl:
            return "SL"
        if hit_tp:
            return "TP"
    return None


def process(sym, cfg, candles, state, now):
    open_, last_sig = state["open"], state["last_sig"]
    d = cfg["digits"]

    # 1) ochiq signalni tekshirish
    s = open_.get(sym)
    if s:
        res = check_open(s, candles)
        if res == "TP":
            send(f"✅ {sym} {s['side']}: TP urildi ({s['tp']})")
            del open_[sym]
        elif res == "SL":
            send(f"❌ {sym} {s['side']}: SL urildi ({s['sl']})")
            del open_[sym]

    # 2) yangi signal qidirish (oxirgi LOOKBACK ta yopilgan sham)
    closed = candles[:-1]
    if len(closed) < 250:
        print(sym, "ma'lumot yetarli emas")
        return
    found, rsi_now, up, down = find_signals([c["c"] for c in closed], LOOKBACK)
    print(f"{sym}: Trend: {'UP' if up else 'DOWN' if down else 'FLAT'} | RSI: {rsi_now:.1f} "
          f"-> {'signal topildi' if found else 'signal yoq'}")

    for i, side, info in found:
        bar = closed[i]
        if bar["t"] <= last_sig.get(sym, 0):
            continue                         # bu signal allaqachon ko'rib chiqilgan
        last_sig[sym] = bar["t"]
        if sym in open_:
            continue                         # shu simvolda ochiq signal bor

        live = candles[-1]["c"]
        age_min = int((now - (bar["t"] + CANDLE_SEC)) / 60)
        move = live - bar["c"]
        # narx signal yo'nalishida yoki teskarisida juda yurib ketgan bo'lsa, eskirgan deb o'tkazamiz
        if abs(move) > MAX_MOVE_FRAC * cfg["sl"]:
            print(f"{sym}: {side} signal eskirgan (narx {move:+.{d}f} yurgan), yuborilmadi")
            continue

        sign = 1 if side == "BUY" else -1
        tp = round(live + sign * cfg["tp"], d)
        sl = round(live - sign * cfg["sl"], d)
        open_[sym] = {"side": side, "entry": live, "tp": tp, "sl": sl, "ts": candles[-1]["t"]}
        icon = "🟢" if side == "BUY" else "🔴"
        send(
            f"{icon} {side} {sym}\n"
            f"Kirish (hozirgi narx): {round(live, d)}\n"
            f"TP: {tp}\n"
            f"SL: {sl}\n"
            f"{info}\n"
            f"Signal paydo bo'lganiga: ~{max(age_min, 0)} daqiqa ({round(bar['c'], d)} dan {move:+.{d}f})\n"
            f"Taymfreym: {INTERVAL}\n"
            f"⚠️ Brokerda hozirgi narxni tekshiring. Narx kirishdan ancha uzoqlashgan bo'lsa, kirmang.\n"
            f"Moliyaviy maslahat emas."
        )


def main():
    if not TOKEN or not CHAT_IDS:
        raise SystemExit("TG_TOKEN va TG_CHAT_ID sozlanmagan (GitHub Secrets).")

    state = load_state()
    now = time.time()
    for sym, cfg in SYMBOLS.items():
        try:
            process(sym, cfg, get_candles(cfg["yahoo"]), state, now)
        except Exception as e:
            print(f"{sym} xatosi:", e)
    save_state(state)


if __name__ == "__main__":
    main()
