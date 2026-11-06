"""
Telegram signal boti - GitHub Actions uchun (bir marta ishlab, tugaydi).

Har ishga tushganda:
  1) narxni Yahoo Finance dan oladi
  2) EMA20/50/200 + RSI bo'yicha tahlil qiladi
  3) signal bo'lsa Telegramga yuboradi
  4) ochiq signal TP yoki SL ga yetgan bo'lsa xabar beradi
  5) holatni state.json ga saqlaydi

Maxfiy ma'lumotlar (GitHub Secrets):  TG_TOKEN, TG_CHAT_ID
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

# ===================== SOZLAMALAR =====================
INTERVAL = "15m"
# tp / sl - NARX masofasi. XAUUSD: tp 25 = $25 harakat (0.02 lotda ~ $50 foyda), sl 12.5 (~ $25 zarar)
SYMBOLS = {
    "XAUUSD": {"yahoo": "GC=F",     "tp": 25.0,   "sl": 12.5,   "digits": 2},
    "EURUSD": {"yahoo": "EURUSD=X", "tp": 0.0050, "sl": 0.0025, "digits": 5},
    "GBPUSD": {"yahoo": "GBPUSD=X", "tp": 0.0060, "sl": 0.0030, "digits": 5},
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


def analyze(closes):
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    up = e20[-1] > e50[-1] > e200[-1]
    down = e20[-1] < e50[-1] < e200[-1]
    cross_up = r[-2] < 50 <= r[-1]
    cross_down = r[-2] > 50 >= r[-1]
    info = f"Trend: {'UP' if up else 'DOWN' if down else 'FLAT'} | RSI: {r[-1]:.1f}"
    if up and cross_up:
        return "BUY", info
    if down and cross_down:
        return "SELL", info
    return None, info


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
        s.setdefault("open", {})
        s.setdefault("last_bar", {})
        return s
    except Exception:
        return {"open": {}, "last_bar": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def check_open(sym, s, candles):
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


def main():
    if not TOKEN or not CHAT_IDS:
        raise SystemExit("TG_TOKEN va TG_CHAT_ID sozlanmagan (GitHub Secrets).")

    state = load_state()
    open_, last_bar = state["open"], state["last_bar"]

    for sym, cfg in SYMBOLS.items():
        try:
            candles = get_candles(cfg["yahoo"])
            if len(candles) < 250:
                print(sym, "ma'lumot yetarli emas")
                continue
            d = cfg["digits"]

            # 1) ochiq signalni tekshirish
            s = open_.get(sym)
            if s:
                res = check_open(sym, s, candles)
                if res == "TP":
                    send(f"✅ {sym} {s['side']}: TP urildi ({s['tp']})")
                    del open_[sym]
                elif res == "SL":
                    send(f"❌ {sym} {s['side']}: SL urildi ({s['sl']})")
                    del open_[sym]

            # 2) yangi signal qidirish (faqat yangi yopilgan shamda)
            closed = candles[:-1]
            bar_ts = closed[-1]["t"]
            if last_bar.get(sym) == bar_ts:
                continue
            last_bar[sym] = bar_ts

            side, info = analyze([c["c"] for c in closed])
            print(f"{sym}: {info} -> {side or 'signal yoq'}")

            if side and sym not in open_:
                entry = candles[-1]["c"]
                sign = 1 if side == "BUY" else -1
                tp = round(entry + sign * cfg["tp"], d)
                sl = round(entry - sign * cfg["sl"], d)
                open_[sym] = {"side": side, "entry": entry, "tp": tp, "sl": sl,
                              "ts": candles[-1]["t"]}
                icon = "🟢" if side == "BUY" else "🔴"
                send(
                    f"{icon} {side} {sym}\n"
                    f"Kirish: {round(entry, d)}\n"
                    f"TP: {tp}\n"
                    f"SL: {sl}\n"
                    f"{info}\n"
                    f"Taymfreym: {INTERVAL}\n"
                    f"⚠️ Moliyaviy maslahat emas. Brokeringizdagi narx biroz farq qilishi mumkin."
                )
        except Exception as e:
            print(f"{sym} xatosi:", e)

    save_state(state)


if __name__ == "__main__":
    main()
