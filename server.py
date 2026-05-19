"""
Reversal Bot Pro — server.py (v2, scanner bug fixed)
=====================================================
Upload this to your reversal bot GitHub repo.
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

# ── Manual CORS ───────────────────────────────────────────────────────────────
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

# ── In-memory state ───────────────────────────────────────────────────────────
latest_scan = {
    "top_picks":      [],
    "bull_reversals": [],
    "bear_reversals": [],
    "scanned_at":     None,
    "total_scanned":  0,
}

browser_cache = {
    "bull":       [],
    "bear":       [],
    "top":        [],
    "updated_at": None,
}

scan_lock = threading.Lock()

STABLES = {
    "USDCUSDT","USD1USDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT",
    "USDTUSDT","DAIUSDT","FRAXUSDT","USDDUSDT","USDEUSDT",
}

# ── Binance helpers ───────────────────────────────────────────────────────────

def get_top_coins(limit=200):
    """Get top coins by 24h volume. Tries futures first, then spot."""
    # Try Binance Futures
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            # Must be a list of dicts
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                usdt = [
                    t for t in data
                    if isinstance(t, dict)
                    and str(t.get("symbol", "")).endswith("USDT")
                    and str(t.get("symbol", "")) not in STABLES
                ]
                usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                logging.info(f"Futures API: {len(usdt)} USDT coins")
                return usdt[:limit]
            else:
                logging.warning(f"Futures API unexpected format: {str(data)[:100]}")
    except Exception as e:
        logging.warning(f"Futures API error: {e}")

    # Fallback: Binance Spot
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                usdt = [
                    t for t in data
                    if isinstance(t, dict)
                    and str(t.get("symbol", "")).endswith("USDT")
                    and str(t.get("symbol", "")) not in STABLES
                ]
                usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                logging.info(f"Spot API fallback: {len(usdt)} USDT coins")
                return usdt[:limit]
    except Exception as e:
        logging.warning(f"Spot API fallback error: {e}")

    logging.warning("Both Binance APIs failed — scan aborted")
    return []


def get_klines(symbol, interval="1d", limit=30):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
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
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


# ── Scanner ───────────────────────────────────────────────────────────────────

def score_coin(ticker, klines_1d):
    try:
        symbol = str(ticker.get("symbol", ""))
        price  = float(ticker.get("lastPrice",          ticker.get("lastPrice", 0)) or 0)
        chg24  = float(ticker.get("priceChangePercent", ticker.get("priceChangePercent", 0)) or 0)
        vol24  = float(ticker.get("quoteVolume",        0) or 0)
        high24 = float(ticker.get("highPrice",          price) or price)
        low24  = float(ticker.get("lowPrice",           price) or price)
    except Exception:
        return None

    if not symbol or price <= 0 or symbol in STABLES:
        return None
    if vol24 < 3_000_000:
        return None

    closes_1d = []
    try:
        closes_1d = [float(k[4]) for k in klines_1d if isinstance(k, list) and len(k) > 4]
    except Exception:
        pass

    rsi_daily = calc_rsi(closes_1d) if closes_1d else 50.0

    try:
        highs = [float(k[2]) for k in klines_1d if isinstance(k, list) and len(k) > 2] or [high24]
        lows  = [float(k[3]) for k in klines_1d if isinstance(k, list) and len(k) > 3] or [low24]
    except Exception:
        highs, lows = [high24], [low24]

    high_52w       = max(highs)
    low_52w        = min(lows)
    drop_from_high = ((high_52w - price) / high_52w * 100) if high_52w > 0 else 0
    rise_from_low  = ((price - low_52w)  / low_52w  * 100) if low_52w  > 0 else 0

    score       = 0
    signal      = None
    reasons     = []
    signals_hit = []

    # BULL REVERSAL
    if rsi_daily < 35 or chg24 < -8 or drop_from_high > 25:
        if rsi_daily < 35:
            score += 20; signals_hit.append("RSI Oversold")
            reasons.append(f"RSI={rsi_daily:.0f}")
        if rsi_daily < 25:
            score += 10; signals_hit.append("Extreme Oversold")
        if drop_from_high > 25:
            score += 15; reasons.append(f"Down {drop_from_high:.0f}% from high")
        if chg24 < -8:
            score += 10; reasons.append(f"{chg24:.1f}% today")
        if chg24 < -15:
            score += 8
        if vol24 > 30_000_000 and chg24 < -5:
            score += 8; signals_hit.append("High Volume Dip")
        if score >= 25:
            signal      = "BULL_REVERSAL"
            probability = min(28 + score, 90)

    # BEAR REVERSAL
    elif rsi_daily > 65 or chg24 > 10:
        if rsi_daily > 75:
            score += 20; signals_hit.append("RSI Overbought")
            reasons.append(f"RSI={rsi_daily:.0f}")
        elif rsi_daily > 65:
            score += 10; signals_hit.append("RSI High")
        if chg24 > 30:
            score += 25; reasons.append(f"+{chg24:.0f}% EXTREME")
        elif chg24 > 20:
            score += 18; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 15:
            score += 12; reasons.append(f"+{chg24:.0f}% today")
        elif chg24 > 10:
            score += 6;  reasons.append(f"+{chg24:.0f}% today")
        if rise_from_low > 30:
            score += 10; reasons.append(f"Up {rise_from_low:.0f}% from low")
        if vol24 > 30_000_000 and chg24 > 15:
            score += 8; signals_hit.append("High Volume Pump")
        if score >= 25:
            signal      = "BEAR_REVERSAL"
            probability = min(28 + score, 90)

    if not signal:
        return None

    grade = ("S" if probability >= 88 else
             "A" if probability >= 78 else
             "B" if probability >= 68 else "C")

    return {
        "symbol":               symbol,
        "probability":          round(probability, 1),
        "signal":               signal,
        "grade":                grade,
        "price":                price,
        "rsi_daily":            rsi_daily,
        "change_24h":           chg24,
        "volume_24h_usd":       vol24,
        "drop_from_high":       round(drop_from_high, 1),
        "rise_from_low":        round(rise_from_low, 1),
        "signals_hit":          signals_hit,
        "reasons":              reasons[:3],
        "divergence":           "regular" if (rsi_daily < 35 and chg24 > 0) else None,
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
    start   = time.time()
    tickers = get_top_coins(200)

    if not tickers:
        logging.warning("No tickers — scan aborted")
        return

    bull_results = []
    bear_results = []
    scanned      = 0

    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        symbol = str(ticker.get("symbol", ""))
        if symbol in STABLES:
            continue
        try:
            klines = get_klines(symbol, "1d", 30)
            result = score_coin(ticker, klines)
            scanned += 1
            if result:
                if result["signal"] == "BULL_REVERSAL":
                    bull_results.append(result)
                elif result["signal"] == "BEAR_REVERSAL":
                    bear_results.append(result)
            time.sleep(0.05)
        except Exception as e:
            logging.debug(f"score_coin error {symbol}: {e}")
            continue

    bull_results.sort(key=lambda x: x["probability"], reverse=True)
    bear_results.sort(key=lambda x: x["probability"], reverse=True)
    top = [r for r in (bull_results + bear_results) if r["grade"] in ("S", "A")]
    top.sort(key=lambda x: x["probability"], reverse=True)

    elapsed = round(time.time() - start, 1)
    with scan_lock:
        latest_scan["bull_reversals"] = bull_results[:20]
        latest_scan["bear_reversals"] = bear_results[:20]
        latest_scan["top_picks"]      = top[:10]
        latest_scan["scanned_at"]     = datetime.datetime.now().isoformat()
        latest_scan["total_scanned"]  = scanned

    logging.info(
        f"✅ Scan done {elapsed}s | {scanned} coins | "
        f"bull={len(bull_results)} bear={len(bear_results)}"
    )


def scan_scheduler():
    while True:
        try:
            run_scan()
        except Exception as e:
            logging.error(f"Scan error: {e}")
        time.sleep(30 * 60)


# ── API Routes ────────────────────────────────────────────────────────────────

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
        if not cached:
            all_bc = browser_cache.get("bull", []) + browser_cache.get("bear", [])
            cached = [x for x in all_bc if x.get("grade", "C") in ("S", "A")]
        if cached:
            logging.info(f"/api/top: {len(cached)} from browser cache")
            return jsonify(cached)
    return jsonify(data or [])


@app.route("/api/bull", methods=["GET"])
def api_bull():
    with scan_lock:
        data = list(latest_scan["bull_reversals"])
    if not data:
        cached = browser_cache.get("bull", [])
        if cached:
            logging.info(f"/api/bull: {len(cached)} from browser cache")
            return jsonify(cached)
    return jsonify(data or [])


@app.route("/api/bear", methods=["GET"])
def api_bear():
    with scan_lock:
        data = list(latest_scan["bear_reversals"])
    if not data:
        cached = browser_cache.get("bear", [])
        if cached:
            logging.info(f"/api/bear: {len(cached)} from browser cache")
            return jsonify(cached)
    return jsonify(data or [])


@app.route("/api/alert", methods=["GET", "POST"])
def api_alert():
    if request.method == "POST":
        try:
            data = request.get_json(force=True, silent=True) or {}
            sym  = str(data.get("symbol", "")).strip().upper()
            prob = float(data.get("probability", data.get("prob", 50)) or 50)
            sig  = str(data.get("signal", "")).upper()
            chg  = float(data.get("change", 0) or 0)

            if sym and prob >= 45:
                if not sym.endswith("USDT"):
                    sym += "USDT"
                if not sig:
                    sig = "BULL_REVERSAL" if chg < 0 else "BEAR_REVERSAL"
                grade = ("S" if prob >= 88 else "A" if prob >= 78
                         else "B" if prob >= 68 else "C")
                entry = {
                    "symbol":               sym,
                    "probability":          prob,
                    "signal":               sig,
                    "grade":                grade,
                    "price":                float(data.get("price", 0) or 0),
                    "rsi_daily":            float(data.get("rsi", 50) or 50),
                    "volume_24h_usd":       float(data.get("volume", 10_000_000) or 10_000_000),
                    "reasons":              data.get("reasons", []),
                    "signals_hit":          data.get("signals", []),
                    "divergence":           data.get("divergence"),
                    "choch_detected":       bool(data.get("choch", False)),
                    "absorption":           bool(data.get("absorption", False)),
                    "squeeze_score":        float(data.get("squeeze", 0) or 0),
                    "timeframes_confirmed": data.get("timeframes", []),
                    "volume_ratio":         1.0,
                    "source":               "browser_dashboard",
                    "detected_at":          datetime.datetime.now().isoformat(),
                }
                key = "bull" if "BULL" in sig else "bear"
                browser_cache[key] = [x for x in browser_cache[key] if x["symbol"] != sym]
                browser_cache[key].append(entry)
                browser_cache[key].sort(key=lambda x: x["probability"], reverse=True)
                browser_cache[key] = browser_cache[key][:30]
                if grade in ("S", "A"):
                    browser_cache["top"] = [x for x in browser_cache["top"] if x["symbol"] != sym]
                    browser_cache["top"].append(entry)
                browser_cache["updated_at"] = datetime.datetime.now().isoformat()
                total = len(browser_cache["bull"]) + len(browser_cache["bear"])
                logging.info(f"Alert: {sym} {sig} {prob:.0f}% | cache={total}")
                return jsonify({"status": "stored", "symbol": sym, "grade": grade})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    all_alerts = browser_cache["bull"] + browser_cache["bear"]
    all_alerts.sort(key=lambda x: x.get("probability", 0), reverse=True)
    return jsonify(all_alerts)


@app.route("/api/signals/push", methods=["POST"])
def push_signals():
    """Dashboard JS posts ALL signals here every 60s."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        now  = datetime.datetime.now().isoformat()
        updated = []

        for key in ("bull", "bear", "top"):
            if key in data and isinstance(data[key], list):
                normalized = []
                for item in data[key]:
                    if not isinstance(item, dict):
                        continue
                    sym = str(item.get("symbol", "")).strip().upper()
                    if not sym:
                        continue
                    if not sym.endswith("USDT"):
                        sym += "USDT"
                    prob = float(
                        item.get("probability",
                        item.get("prob",
                        item.get("reversal_probability", 50))) or 50
                    )
                    if prob < 45:
                        continue
                    grade = item.get("grade") or (
                        "S" if prob>=88 else "A" if prob>=78 else "B" if prob>=68 else "C"
                    )
                    sig = item.get("signal") or (
                        "BULL_REVERSAL" if key == "bull" else "BEAR_REVERSAL"
                    )
                    normalized.append({
                        **item,
                        "symbol":      sym,
                        "probability": prob,
                        "grade":       grade,
                        "signal":      sig,
                        "source":      "browser_dashboard",
                        "detected_at": now,
                    })
                browser_cache[key] = normalized
                updated.append(f"{key}={len(normalized)}")

        browser_cache["updated_at"] = now
        total = len(browser_cache["bull"]) + len(browser_cache["bear"])
        logging.info(f"Signal push: {', '.join(updated)} | total={total}")
        return jsonify({"status": "ok", "received": updated, "total": total, "updated_at": now})

    except Exception as e:
        logging.error(f"push_signals error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


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
        "name":    "Reversal Bot Pro API",
        "status":  "running",
        "endpoints": [
            "/api/bull", "/api/bear", "/api/top",
            "/api/scan", "/api/scan/now",
            "/api/alert",
            "/api/signals/push",
            "/api/signals/status",
            "/health",
        ]
    })


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=scan_scheduler, daemon=True).start()
    logging.info("🚀 Reversal Bot Pro server starting...")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
