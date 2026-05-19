"""
Reversal Bot Pro — server.py (v3, lower thresholds)
=====================================================
Upload to reversal bot GitHub repo.

Key fix: Scanner thresholds lowered to match what the dashboard already shows.
Dashboard finds MU at -5.78% (68%), LAB at -3.37% (68%) — scanner now finds these too.
"""

import os
import time
import logging
import threading
import datetime
import requests
from flask import Flask, request, jsonify, Response

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path=''):
    r = Response()
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    return r, 200

# ── State ─────────────────────────────────────────────────────────────────────
latest_scan = {
    "top_picks":      [],
    "bull_reversals": [],
    "bear_reversals": [],
    "scanned_at":     None,
    "total_scanned":  0,
}
browser_cache = {
    "bull": [], "bear": [], "top": [], "updated_at": None,
}
scan_lock = threading.Lock()

STABLES = {
    "USDCUSDT","USD1USDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT",
    "USDTUSDT","DAIUSDT","FRAXUSDT","USDDUSDT","USDEUSDT",
    "USDPUSDT","USTCUSDT","EURUSDT","GBPUSDT","AEURUSDT",
}

# ── Binance helpers ───────────────────────────────────────────────────────────
def get_top_coins(limit=300):
    """Try futures, fall back to spot. Validate response is a list."""
    for url in [
        "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
    ]:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list) or not data:
                logging.warning(f"Unexpected response from {url}: {str(data)[:80]}")
                continue
            if not isinstance(data[0], dict):
                logging.warning(f"Response items not dicts: {str(data[0])[:80]}")
                continue
            usdt = [
                t for t in data
                if isinstance(t, dict)
                and str(t.get("symbol","")).endswith("USDT")
                and str(t.get("symbol","")) not in STABLES
                and float(t.get("quoteVolume", 0) or 0) > 1_000_000
            ]
            usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
            source = "Futures" if "fapi" in url else "Spot"
            logging.info(f"✅ {source} API: {len(usdt)} USDT coins")
            return usdt[:limit]
        except Exception as e:
            logging.warning(f"get_top_coins {url}: {e}")
    logging.error("Both Binance APIs failed")
    return []

def get_klines(symbol, interval="1d", limit=30):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8
        )
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return round(100 - (100 / (1 + ag / al)), 2) if al > 0 else 100.0

# ── Scanner — thresholds tuned to match dashboard ─────────────────────────────
def score_coin(ticker, klines_1d):
    try:
        symbol = str(ticker.get("symbol",""))
        price  = float(ticker.get("lastPrice", 0) or 0)
        chg24  = float(ticker.get("priceChangePercent", 0) or 0)
        vol24  = float(ticker.get("quoteVolume", 0) or 0)
        high24 = float(ticker.get("highPrice", price) or price)
        low24  = float(ticker.get("lowPrice",  price) or price)
    except Exception:
        return None

    if not symbol or price <= 0 or symbol in STABLES or vol24 < 1_000_000:
        return None

    # RSI from daily klines
    closes = []
    try:
        closes = [float(k[4]) for k in klines_1d
                  if isinstance(k, list) and len(k) > 4]
    except Exception:
        pass
    rsi = calc_rsi(closes) if closes else 50.0

    # Distance from highs/lows
    try:
        highs = [float(k[2]) for k in klines_1d if isinstance(k, list) and len(k) > 2] or [high24]
        lows  = [float(k[3]) for k in klines_1d if isinstance(k, list) and len(k) > 3] or [low24]
    except Exception:
        highs, lows = [high24], [low24]

    h52 = max(highs); l52 = min(lows)
    drop = ((h52 - price) / h52 * 100) if h52 > 0 else 0
    rise = ((price - l52)  / l52  * 100) if l52  > 0 else 0

    score = 0; signal = None; reasons = []; hits = []

    # ── BULL REVERSAL ─────────────────────────────────────────────────────
    # Lowered thresholds: catches MU (-5.78%), LAB (-3.37%), UB (-6.78%)
    bull_eligible = (
        chg24 < -2 or          # Any dip > 2%
        rsi < 45 or            # RSI below neutral (was 35)
        drop > 10              # Down 10%+ from high (was 25%)
    )
    if bull_eligible:
        # Scoring
        if rsi < 25:   score += 30; hits.append("Extreme Oversold")
        elif rsi < 35: score += 22; hits.append("RSI Oversold")
        elif rsi < 45: score += 12; hits.append("RSI Below Neutral")
        elif rsi < 50: score += 5

        if chg24 < -15: score += 20; reasons.append(f"{chg24:.1f}% crash")
        elif chg24 < -8: score += 14; reasons.append(f"{chg24:.1f}% today")
        elif chg24 < -5: score += 10; reasons.append(f"{chg24:.1f}% today")
        elif chg24 < -3: score += 6;  reasons.append(f"{chg24:.1f}% today")
        elif chg24 < -2: score += 3;  reasons.append(f"{chg24:.1f}% today")

        if drop > 40: score += 20; reasons.append(f"Down {drop:.0f}% from high")
        elif drop > 25: score += 14; reasons.append(f"Down {drop:.0f}% from high")
        elif drop > 15: score += 8;  reasons.append(f"Down {drop:.0f}% from high")
        elif drop > 10: score += 4;  reasons.append(f"Down {drop:.0f}% from high")

        if vol24 > 30_000_000 and chg24 < -3:
            score += 6; hits.append("High Volume Dip")

        if score >= 10:  # Lowered from 25
            signal = "BULL_REVERSAL"
            prob   = min(35 + score * 1.5, 92)

    # ── BEAR REVERSAL ─────────────────────────────────────────────────────
    # Lowered thresholds: catches RAVE (+10.42%), SPACE (+23.48%)
    bear_eligible = (
        chg24 > 4 or           # Any pump > 4% (was 10%)
        rsi > 58 or            # RSI above neutral (was 65)
        rise > 10              # Up 10%+ from low (was 30%)
    )
    if not bull_eligible and bear_eligible:
        if rsi > 80:   score += 30; hits.append("Extreme Overbought")
        elif rsi > 70: score += 22; hits.append("RSI Overbought")
        elif rsi > 60: score += 12; hits.append("RSI Elevated")
        elif rsi > 55: score += 5

        if chg24 > 40: score += 25; reasons.append(f"+{chg24:.0f}% EXTREME")
        elif chg24 > 25: score += 18; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 15: score += 12; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 8:  score += 8;  reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 4:  score += 4;  reasons.append(f"+{chg24:.0f}% today")

        if rise > 50: score += 20; reasons.append(f"Up {rise:.0f}% from low")
        elif rise > 30: score += 14; reasons.append(f"Up {rise:.0f}% from low")
        elif rise > 15: score += 8;  reasons.append(f"Up {rise:.0f}% from low")
        elif rise > 10: score += 4;  reasons.append(f"Up {rise:.0f}% from low")

        if vol24 > 30_000_000 and chg24 > 8:
            score += 6; hits.append("High Volume Pump")

        if score >= 10:  # Lowered from 25
            signal = "BEAR_REVERSAL"
            prob   = min(35 + score * 1.5, 92)

    if not signal:
        return None

    grade = ("S" if prob >= 88 else "A" if prob >= 78
             else "B" if prob >= 68 else "C" if prob >= 55 else "D")

    return {
        "symbol":               symbol,
        "probability":          round(prob, 1),
        "signal":               signal,
        "grade":                grade,
        "price":                price,
        "rsi_daily":            rsi,
        "change_24h":           chg24,
        "volume_24h_usd":       vol24,
        "drop_from_high":       round(drop, 1),
        "rise_from_low":        round(rise, 1),
        "signals_hit":          hits,
        "reasons":              reasons[:3],
        "divergence":           "regular" if (rsi < 45 and chg24 > 0) else None,
        "choch_detected":       False,
        "absorption":           False,
        "squeeze_score":        0,
        "timeframes_confirmed": [],
        "volume_ratio":         1.0,
        "source":               "python_scanner",
        "scanned_at":           datetime.datetime.now().isoformat(),
    }


def run_scan():
    logging.info("🔍 Starting Python reversal scan...")
    t0 = time.time()
    tickers = get_top_coins(300)
    if not tickers:
        logging.warning("No tickers — scan aborted")
        return

    bull_r = []; bear_r = []; scanned = 0

    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        sym = str(ticker.get("symbol",""))
        if sym in STABLES:
            continue
        try:
            klines = get_klines(sym, "1d", 30)
            result = score_coin(ticker, klines)
            scanned += 1
            if result:
                if result["signal"] == "BULL_REVERSAL":
                    bull_r.append(result)
                else:
                    bear_r.append(result)
            time.sleep(0.04)
        except Exception as e:
            logging.debug(f"{sym}: {e}")
            continue

    bull_r.sort(key=lambda x: x["probability"], reverse=True)
    bear_r.sort(key=lambda x: x["probability"], reverse=True)
    top = [r for r in bull_r + bear_r if r["grade"] in ("S","A","B")]
    top.sort(key=lambda x: x["probability"], reverse=True)

    with scan_lock:
        latest_scan["bull_reversals"] = bull_r[:25]
        latest_scan["bear_reversals"] = bear_r[:25]
        latest_scan["top_picks"]      = top[:15]
        latest_scan["scanned_at"]     = datetime.datetime.now().isoformat()
        latest_scan["total_scanned"]  = scanned

    logging.info(
        f"✅ Scan done {round(time.time()-t0,1)}s | {scanned} coins | "
        f"bull={len(bull_r)} bear={len(bear_r)}"
    )


def scan_scheduler():
    while True:
        try:
            run_scan()
        except Exception as e:
            logging.error(f"Scan error: {e}")
        time.sleep(25 * 60)  # every 25 minutes

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":          "ok",
        "python_bull":     len(latest_scan["bull_reversals"]),
        "python_bear":     len(latest_scan["bear_reversals"]),
        "browser_bull":    len(browser_cache["bull"]),
        "browser_bear":    len(browser_cache["bear"]),
        "scanned_at":      latest_scan["scanned_at"],
        "browser_updated": browser_cache["updated_at"],
    })

@app.route("/api/scan", methods=["GET"])
def api_scan():
    with scan_lock:
        return jsonify({
            "top_picks":      latest_scan["top_picks"],
            "bull_reversals": latest_scan["bull_reversals"],
            "bear_reversals": latest_scan["bear_reversals"],
            "scanned_at":     latest_scan["scanned_at"],
            "total_scanned":  latest_scan["total_scanned"],
        })

@app.route("/api/scan/now", methods=["GET","POST"])
def api_scan_now():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "scan started"})

@app.route("/api/top", methods=["GET"])
def api_top():
    with scan_lock:
        data = list(latest_scan["top_picks"])
    if not data:
        cached = browser_cache.get("top",[])
        if not cached:
            all_bc = browser_cache.get("bull",[]) + browser_cache.get("bear",[])
            cached = [x for x in all_bc if x.get("grade","D") in ("S","A","B")]
        if cached:
            logging.info(f"/api/top browser cache: {len(cached)}")
            return jsonify(cached)
    return jsonify(data or [])

@app.route("/api/bull", methods=["GET"])
def api_bull():
    with scan_lock:
        data = list(latest_scan["bull_reversals"])
    if not data:
        cached = browser_cache.get("bull",[])
        if cached:
            logging.info(f"/api/bull browser cache: {len(cached)}")
            return jsonify(cached)
    return jsonify(data or [])

@app.route("/api/bear", methods=["GET"])
def api_bear():
    with scan_lock:
        data = list(latest_scan["bear_reversals"])
    if not data:
        cached = browser_cache.get("bear",[])
        if cached:
            logging.info(f"/api/bear browser cache: {len(cached)}")
            return jsonify(cached)
    return jsonify(data or [])

@app.route("/api/alert", methods=["GET","POST"])
def api_alert():
    if request.method == "POST":
        try:
            d   = request.get_json(force=True, silent=True) or {}
            sym = str(d.get("symbol","")).strip().upper()
            prob= float(d.get("probability", d.get("prob", 50)) or 50)
            sig = str(d.get("signal","")).upper()
            chg = float(d.get("change", 0) or 0)
            if sym and prob >= 40:
                if not sym.endswith("USDT"): sym += "USDT"
                if not sig: sig = "BULL_REVERSAL" if chg < 0 else "BEAR_REVERSAL"
                grade = "S" if prob>=88 else "A" if prob>=78 else "B" if prob>=68 else "C"
                entry = {
                    "symbol": sym, "probability": prob, "signal": sig, "grade": grade,
                    "price": float(d.get("price",0) or 0),
                    "rsi_daily": float(d.get("rsi",50) or 50),
                    "volume_24h_usd": float(d.get("volume",10_000_000) or 10_000_000),
                    "reasons": d.get("reasons",[]), "signals_hit": d.get("signals",[]),
                    "divergence": d.get("divergence"), "choch_detected": bool(d.get("choch")),
                    "absorption": bool(d.get("absorption")), "squeeze_score": float(d.get("squeeze",0) or 0),
                    "timeframes_confirmed": d.get("timeframes",[]), "volume_ratio": 1.0,
                    "source": "browser_dashboard",
                    "detected_at": datetime.datetime.now().isoformat(),
                }
                key = "bull" if "BULL" in sig else "bear"
                browser_cache[key] = [x for x in browser_cache[key] if x["symbol"]!=sym]
                browser_cache[key].append(entry)
                browser_cache[key].sort(key=lambda x: x["probability"], reverse=True)
                browser_cache[key] = browser_cache[key][:30]
                if grade in ("S","A","B"):
                    browser_cache["top"] = [x for x in browser_cache["top"] if x["symbol"]!=sym]
                    browser_cache["top"].append(entry)
                browser_cache["updated_at"] = datetime.datetime.now().isoformat()
                total = len(browser_cache["bull"]) + len(browser_cache["bear"])
                logging.info(f"Alert: {sym} {sig} {prob:.0f}% | cache={total}")
                return jsonify({"status":"stored","symbol":sym,"grade":grade})
        except Exception as e:
            return jsonify({"status":"error","message":str(e)}), 500
    all_a = browser_cache["bull"] + browser_cache["bear"]
    all_a.sort(key=lambda x: x.get("probability",0), reverse=True)
    return jsonify(all_a)

@app.route("/api/signals/push", methods=["POST"])
def push_signals():
    try:
        data = request.get_json(force=True, silent=True) or {}
        now  = datetime.datetime.now().isoformat()
        updated = []
        for key in ("bull","bear","top"):
            if key in data and isinstance(data[key], list):
                norm = []
                for item in data[key]:
                    if not isinstance(item, dict): continue
                    sym = str(item.get("symbol","")).strip().upper()
                    if not sym: continue
                    if not sym.endswith("USDT"): sym += "USDT"
                    prob = float(item.get("probability", item.get("prob",
                                 item.get("reversal_probability",50))) or 50)
                    if prob < 40: continue
                    grade = item.get("grade") or (
                        "S" if prob>=88 else "A" if prob>=78 else "B" if prob>=68 else "C")
                    sig = item.get("signal") or (
                        "BULL_REVERSAL" if key=="bull" else "BEAR_REVERSAL")
                    norm.append({**item, "symbol":sym, "probability":prob,
                                 "grade":grade, "signal":sig,
                                 "source":"browser_dashboard", "detected_at":now})
                browser_cache[key] = norm
                updated.append(f"{key}={len(norm)}")
        browser_cache["updated_at"] = now
        total = len(browser_cache["bull"]) + len(browser_cache["bear"])
        logging.info(f"Signal push: {', '.join(updated)} | total={total}")
        return jsonify({"status":"ok","received":updated,"total":total,"updated_at":now})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/api/signals/status", methods=["GET"])
def signals_status():
    return jsonify({
        "bull_count":  len(browser_cache["bull"]),
        "bear_count":  len(browser_cache["bear"]),
        "top_count":   len(browser_cache["top"]),
        "updated_at":  browser_cache["updated_at"],
        "python_bull": len(latest_scan["bull_reversals"]),
        "python_bear": len(latest_scan["bear_reversals"]),
        "scanned_at":  latest_scan["scanned_at"],
        "top_3_bull":  browser_cache["bull"][:3],
        "top_3_bear":  browser_cache["bear"][:3],
    })

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Reversal Bot Pro API", "status": "running",
        "python_bull": len(latest_scan["bull_reversals"]),
        "python_bear": len(latest_scan["bear_reversals"]),
        "browser_bull": len(browser_cache["bull"]),
        "browser_bear": len(browser_cache["bear"]),
    })

if __name__ == "__main__":
    threading.Thread(target=scan_scheduler, daemon=True).start()
    logging.info("🚀 Reversal Bot Pro server starting...")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
