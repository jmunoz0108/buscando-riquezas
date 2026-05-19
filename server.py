"""
Reversal Bot Pro — server.py
Complete file — upload directly to GitHub, replaces existing server.py
"""

import os
import json
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

# ── Manual CORS (no flask_cors package needed) ────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,OPTIONS'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path=''):
    r = Response()
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,OPTIONS'
    return r, 200
# ─────────────────────────────────────────────────────────────────────────────

# ── In-memory state ───────────────────────────────────────────────────────────
# Python scanner results (filled every 30 min)
latest_scan = {
    "top_picks":      [],
    "bull_reversals": [],
    "bear_reversals": [],
    "scanned_at":     None,
    "total_scanned":  0,
}

# Browser cache — filled by dashboard JS via POST /api/signals/push
# Warfare bot reads /api/bull, /api/bear, /api/top which fall back here
browser_cache = {
    "bull":        [],
    "bear":        [],
    "top":         [],
    "updated_at":  None,
}

scan_lock = threading.Lock()

# ── Binance helpers ───────────────────────────────────────────────────────────
BINANCE_FUTURES = "https://fapi.binance.com"
BINANCE_SPOT    = "https://api.binance.com"

STABLES = {
    "USDCUSDT","USD1USDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT",
    "USDTUSDT","DAIUSDT","FRAXUSDT","USDDUSDT","USDEUSDT"
}

def get_top_coins(limit=200):
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr", timeout=15)
        tickers = r.json()
        usdt = [t for t in tickers if t.get("symbol","").endswith("USDT")]
        usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        return usdt[:limit]
    except Exception as e:
        logging.warning(f"get_top_coins: {e}")
        return []

def get_klines(symbol, interval="1d", limit=30):
    try:
        r = requests.get(
            f"{BINANCE_SPOT}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        return r.json()
    except Exception:
        return []

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)

# ── Python scanner ────────────────────────────────────────────────────────────
def score_coin(ticker, klines_1d):
    symbol = ticker.get("symbol", "")
    price  = float(ticker.get("lastPrice", 0))
    chg24  = float(ticker.get("priceChangePercent", 0))
    vol24  = float(ticker.get("quoteVolume", 0))
    high24 = float(ticker.get("highPrice", price))
    low24  = float(ticker.get("lowPrice",  price))

    if not symbol or price <= 0 or symbol in STABLES:
        return None
    if vol24 < 3_000_000:
        return None

    closes_1d = [float(k[4]) for k in klines_1d] if klines_1d else []
    rsi_daily = calc_rsi(closes_1d) if closes_1d else 50.0

    highs = [float(k[2]) for k in klines_1d] if klines_1d else [high24]
    lows  = [float(k[3]) for k in klines_1d] if klines_1d else [low24]
    high_52w = max(highs) if highs else high24
    low_52w  = min(lows)  if lows  else low24

    drop_from_high = ((high_52w - price) / high_52w * 100) if high_52w > 0 else 0
    rise_from_low  = ((price - low_52w)  / low_52w  * 100) if low_52w  > 0 else 0

    score   = 0
    signal  = None
    reasons = []
    signals_hit = []

    # BULL REVERSAL
    if rsi_daily < 35:
        score += 20
        signals_hit.append("RSI Oversold")
        reasons.append(f"RSI={rsi_daily:.0f} oversold")
    if drop_from_high > 20:
        score += 15
        reasons.append(f"Down {drop_from_high:.0f}% from high")
    if chg24 < -5:
        score += 10
        reasons.append(f"{chg24:.1f}% today")
    if rsi_daily < 25:
        score += 10
        signals_hit.append("Extreme Oversold")
    if chg24 < -10:
        score += 10
    if vol24 > 50_000_000 and chg24 < -5:
        score += 8
        signals_hit.append("High Volume Dip")

    if score >= 28:
        signal = "BULL_REVERSAL"
        probability = min(30 + score, 88)
    elif rsi_daily > 70 or chg24 > 8:
        score = 0
        signals_hit = []
        reasons = []
        if rsi_daily > 75:
            score += 20; signals_hit.append("RSI Overbought"); reasons.append(f"RSI={rsi_daily:.0f}")
        elif rsi_daily > 70:
            score += 12; signals_hit.append("RSI High")
        if chg24 > 30:
            score += 25; reasons.append(f"+{chg24:.0f}% EXTREME")
        elif chg24 > 20:
            score += 18; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 15:
            score += 12; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 8:
            score += 6
        if rise_from_low > 30:
            score += 10; reasons.append(f"Up {rise_from_low:.0f}% from low")
        if vol24 > 50_000_000 and chg24 > 15:
            score += 8; signals_hit.append("High Volume Pump")
        if score >= 25:
            signal = "BEAR_REVERSAL"
            probability = min(30 + score, 88)
        else:
            return None
    else:
        return None

    if not signal:
        return None

    grade = ("S" if probability >= 88 else "A" if probability >= 78
             else "B" if probability >= 68 else "C" if probability >= 58 else "D")

    return {
        "symbol":           symbol,
        "probability":      probability,
        "signal":           signal,
        "grade":            grade,
        "price":            price,
        "rsi_daily":        rsi_daily,
        "change_24h":       chg24,
        "volume_24h_usd":   vol24,
        "drop_from_high":   drop_from_high,
        "rise_from_low":    rise_from_low,
        "signals_hit":      signals_hit,
        "reasons":          reasons[:3],
        "divergence":       "regular" if rsi_daily < 35 and chg24 > 0 else None,
        "choch_detected":   False,
        "absorption":       False,
        "squeeze_score":    0,
        "timeframes_confirmed": [],
        "volume_ratio":     1.0,
        "source":           "python_scanner",
        "scanned_at":       datetime.datetime.now().isoformat(),
    }


def run_scan():
    logging.info("Starting Python reversal scan...")
    start   = time.time()
    tickers = get_top_coins(200)
    if not tickers:
        logging.warning("No tickers — scan aborted")
        return

    bull_results = []
    bear_results = []
    scanned = 0

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if symbol in STABLES:
            continue
        try:
            klines = get_klines(symbol, "1d", 30)
            result = score_coin(ticker, klines)
            scanned += 1
            if result:
                if result["signal"] == "BULL_REVERSAL":
                    bull_results.append(result)
                else:
                    bear_results.append(result)
            time.sleep(0.05)
        except Exception:
            continue

    bull_results.sort(key=lambda x: x["probability"], reverse=True)
    bear_results.sort(key=lambda x: x["probability"], reverse=True)
    top = [r for r in bull_results + bear_results if r["grade"] in ("S","A")]
    top.sort(key=lambda x: x["probability"], reverse=True)

    elapsed = round(time.time() - start, 1)
    with scan_lock:
        latest_scan["bull_reversals"] = bull_results[:20]
        latest_scan["bear_reversals"] = bear_results[:20]
        latest_scan["top_picks"]      = top[:10]
        latest_scan["scanned_at"]     = datetime.datetime.now().isoformat()
        latest_scan["total_scanned"]  = scanned

    logging.info(f"Scan done in {elapsed}s: {scanned} coins | "
                 f"bull={len(bull_results)} bear={len(bear_results)}")


def scan_scheduler():
    while True:
        try:
            run_scan()
        except Exception as e:
            logging.error(f"Scan error: {e}")
        time.sleep(30 * 60)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":        "ok",
        "scanned_at":    latest_scan["scanned_at"],
        "python_bull":   len(latest_scan["bull_reversals"]),
        "python_bear":   len(latest_scan["bear_reversals"]),
        "browser_bull":  len(browser_cache["bull"]),
        "browser_bear":  len(browser_cache["bear"]),
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


@app.route("/api/scan/now", methods=["GET", "POST"])
def api_scan_now():
    def _bg():
        try:
            run_scan()
        except Exception as e:
            logging.error(f"bg scan: {e}")
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "scan started"})


@app.route("/api/top", methods=["GET"])
def api_top():
    with scan_lock:
        data = list(latest_scan["top_picks"])
    if not data:
        cached = browser_cache.get("top", [])
        if cached:
            logging.info(f"/api/top: {len(cached)} browser-cached signals")
            return jsonify(cached)
        merged = [x for x in browser_cache.get("bull",[]) + browser_cache.get("bear",[])
                  if x.get("grade","C") in ("S","A")]
        if merged:
            return jsonify(merged)
    return jsonify(data or [])


@app.route("/api/bull", methods=["GET"])
def api_bull():
    with scan_lock:
        data = list(latest_scan["bull_reversals"])
    if not data:
        cached = browser_cache.get("bull", [])
        if cached:
            logging.info(f"/api/bull: {len(cached)} browser-cached signals")
            return jsonify(cached)
    return jsonify(data or [])


@app.route("/api/bear", methods=["GET"])
def api_bear():
    with scan_lock:
        data = list(latest_scan["bear_reversals"])
    if not data:
        cached = browser_cache.get("bear", [])
        if cached:
            logging.info(f"/api/bear: {len(cached)} browser-cached signals")
            return jsonify(cached)
    return jsonify(data or [])


@app.route("/api/alert", methods=["GET", "POST"])
def api_alert():
    """Called by dashboard JS per-signal. Stores in browser cache."""
    if request.method == "POST":
        try:
            data = request.get_json(force=True, silent=True) or {}
            sym  = data.get("symbol", "")
            prob = float(data.get("probability", data.get("prob", 50)) or 50)
            sig  = data.get("signal", "")

            if sym and prob >= 45:
                if not sym.endswith("USDT"):
                    sym += "USDT"
                grade = ("S" if prob>=88 else "A" if prob>=78
                         else "B" if prob>=68 else "C")
                if not sig:
                    chg = float(data.get("change", data.get("change_24h", 0)) or 0)
                    sig = "BULL_REVERSAL" if chg < 0 else "BEAR_REVERSAL"

                entry = {
                    "symbol":           sym,
                    "probability":      prob,
                    "signal":           sig,
                    "grade":            grade,
                    "price":            float(data.get("price", 0) or 0),
                    "rsi_daily":        float(data.get("rsi", 50) or 50),
                    "volume_24h_usd":   float(data.get("volume", 10_000_000) or 10_000_000),
                    "reasons":          data.get("reasons", []),
                    "signals_hit":      data.get("signals", []),
                    "divergence":       data.get("divergence"),
                    "choch_detected":   bool(data.get("choch", False)),
                    "absorption":       bool(data.get("absorption", False)),
                    "squeeze_score":    float(data.get("squeeze", 0) or 0),
                    "timeframes_confirmed": data.get("timeframes", []),
                    "volume_ratio":     1.0,
                    "source":           "browser_dashboard",
                    "detected_at":      datetime.datetime.now().isoformat(),
                }

                key = "bull" if "BULL" in sig else "bear"
                browser_cache[key] = [x for x in browser_cache[key] if x["symbol"] != sym]
                browser_cache[key].append(entry)
                browser_cache[key].sort(key=lambda x: x["probability"], reverse=True)
                browser_cache[key] = browser_cache[key][:30]

                if grade in ("S", "A"):
                    browser_cache["top"] = [x for x in browser_cache["top"] if x["symbol"] != sym]
                    browser_cache["top"].append(entry)
                    browser_cache["top"].sort(key=lambda x: x["probability"], reverse=True)

                browser_cache["updated_at"] = datetime.datetime.now().isoformat()

                logging.info(f"Alert stored: {sym} {sig} {prob:.0f}% [{grade}] "
                             f"| bull={len(browser_cache['bull'])} bear={len(browser_cache['bear'])}")

                return jsonify({"status": "stored", "symbol": sym, "grade": grade})

        except Exception as e:
            logging.warning(f"alert POST error: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    # GET — return all alerts
    all_alerts = browser_cache["bull"] + browser_cache["bear"]
    all_alerts.sort(key=lambda x: x.get("probability", 0), reverse=True)
    return jsonify(all_alerts)


@app.route("/api/signals/push", methods=["POST"])
def push_signals():
    """Dashboard JS posts bulk signals here every 60s."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        now  = datetime.datetime.now().isoformat()
        updated = []

        for key in ("bull", "bear", "top"):
            if key not in data or not isinstance(data[key], list):
                continue
            items = []
            for item in data[key]:
                if not isinstance(item, dict) or not item.get("symbol"):
                    continue
                sym = item["symbol"]
                if not sym.endswith("USDT"):
                    sym += "USDT"
                item["symbol"]     = sym
                item["source"]     = "browser_dashboard"
                item["detected_at"] = now
                prob = float(item.get("probability", item.get("prob", 50)) or 50)
                item["probability"] = prob
                if "grade" not in item:
                    item["grade"] = ("S" if prob>=88 else "A" if prob>=78
                                     else "B" if prob>=68 else "C")
                if "signal" not in item:
                    item["signal"] = ("BULL_REVERSAL" if key == "bull"
                                      else "BEAR_REVERSAL")
                items.append(item)
            browser_cache[key] = items
            updated.append(f"{key}={len(items)}")

        browser_cache["updated_at"] = now
        total = len(browser_cache["bull"]) + len(browser_cache["bear"])
        logging.info(f"Signal push: {', '.join(updated)} | total={total}")

        return jsonify({
            "status":     "ok",
            "received":   updated,
            "total":      total,
            "updated_at": now,
        })

    except Exception as e:
        logging.error(f"push_signals error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/signals/status", methods=["GET"])
def signals_status():
    """Warfare bot polls this to check if browser cache has data."""
    return jsonify({
        "bull_count":    len(browser_cache["bull"]),
        "bear_count":    len(browser_cache["bear"]),
        "top_count":     len(browser_cache["top"]),
        "updated_at":    browser_cache["updated_at"],
        "python_bull":   len(latest_scan["bull_reversals"]),
        "python_bear":   len(latest_scan["bear_reversals"]),
        "python_scanned_at": latest_scan["scanned_at"],
        "top_3_bull":    browser_cache["bull"][:3],
        "top_3_bear":    browser_cache["bear"][:3],
    })


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Reversal Bot Pro API",
        "status": "running",
        "python_bull":  len(latest_scan["bull_reversals"]),
        "python_bear":  len(latest_scan["bear_reversals"]),
        "browser_bull": len(browser_cache["bull"]),
        "browser_bear": len(browser_cache["bear"]),
        "endpoints": [
            "GET  /api/bull         — bull reversal signals",
            "GET  /api/bear         — bear reversal signals",
            "GET  /api/top          — top S+A grade picks",
            "GET  /api/scan         — full scan result",
            "POST /api/scan/now     — trigger immediate scan",
            "POST /api/alert        — store one signal (dashboard JS)",
            "POST /api/signals/push — store bulk signals (dashboard JS)",
            "GET  /api/signals/status — cache health check",
            "GET  /health           — server health",
        ]
    })


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Run Python scanner in background immediately then every 30 min
    threading.Thread(target=scan_scheduler, daemon=True).start()
    logging.info("Reversal Bot Pro server starting on port 5000")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
