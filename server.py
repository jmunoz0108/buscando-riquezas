"""
Reversal Bot Pro — server.py
============================
Flask API server that:
1. Runs the Python scanner every 30 minutes
2. Accepts signals pushed from the dashboard browser JS
3. Serves /api/bull, /api/bear, /api/top, /api/alert
   (returns browser-detected signals when Python scanner finds 0)

The warfare bot reads from this server.
The dashboard JS posts to /api/signals/push every 60s.
"""

import os
import json
import time
import logging
import threading
import datetime
import requests

from flask import Flask, request, jsonify
from flask_cors import CORS

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

app = Flask(__name__)
CORS(app)

# ── In-memory state ──────────────────────────────────────────────────────────
# Python scanner results (filled every 30 min)
latest_scan = {
    "top_picks":      [],
    "bull_reversals": [],
    "bear_reversals": [],
    "scanned_at":     None,
    "total_scanned":  0,
}

# Browser cache (filled by dashboard JS via POST /api/signals/push)
browser_cache = {
    "bull":        [],
    "bear":        [],
    "top":         [],
    "candle_bull": [],
    "candle_bear": [],
    "updated_at":  None,
}

scan_lock = threading.Lock()

# ── Binance helpers ──────────────────────────────────────────────────────────
BINANCE_FUTURES = "https://fapi.binance.com"
BINANCE_SPOT    = "https://api.binance.com"

def get_top_coins(limit=200):
    """Get top coins by futures volume."""
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
    """Fetch klines from Binance spot."""
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
    """Wilder RSI."""
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
    return round(100 - (100 / (1 + avg_g/avg_l)), 2)

# ── Python scanner ───────────────────────────────────────────────────────────
STABLES = {"USDCUSDT","USD1USDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT","USDTUSDT",
           "DAIUSDT","FRAXUSDT","USDDUSDT","USDEUSDT"}

def score_coin(ticker, klines_1d):
    """
    Score a coin for reversal probability.
    Returns (probability, signal, grade, details_dict) or None.
    """
    symbol   = ticker.get("symbol","")
    price    = float(ticker.get("lastPrice", 0))
    chg24    = float(ticker.get("priceChangePercent", 0))
    vol24    = float(ticker.get("quoteVolume", 0))
    high24   = float(ticker.get("highPrice", price))
    low24    = float(ticker.get("lowPrice",  price))

    if not symbol or price <= 0 or symbol in STABLES:
        return None

    # Need at least a bit of volume
    if vol24 < 3_000_000:
        return None

    # Get daily closes for RSI
    closes_1d = [float(k[4]) for k in klines_1d] if klines_1d else []
    rsi_daily = calc_rsi(closes_1d) if closes_1d else 50.0

    # 52-week high/low from daily klines
    highs = [float(k[2]) for k in klines_1d] if klines_1d else [high24]
    lows  = [float(k[3]) for k in klines_1d] if klines_1d else [low24]
    high_52w = max(highs) if highs else high24
    low_52w  = min(lows)  if lows  else low24

    drop_from_high = ((high_52w - price) / high_52w * 100) if high_52w > 0 else 0
    rise_from_low  = ((price - low_52w) / low_52w * 100)  if low_52w  > 0 else 0

    score  = 0
    signal = None
    reasons = []
    signals_hit = []

    # ── BULL REVERSAL (oversold bounce) ─────────────────────────────────
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

    # volume spike (panic selling = reversal signal)
    if vol24 > 50_000_000 and chg24 < -5:
        score += 8
        signals_hit.append("High Volume Dip")

    if score >= 28:
        signal = "BULL_REVERSAL"
        probability = min(30 + score, 88)

    # ── BEAR REVERSAL (overbought pullback) ──────────────────────────────
    elif rsi_daily > 70 or chg24 > 15:
        score = 0
        signals_hit = []
        reasons = []

        if rsi_daily > 75:
            score += 20; signals_hit.append("RSI Overbought"); reasons.append(f"RSI={rsi_daily:.0f}")
        elif rsi_daily > 70:
            score += 12; signals_hit.append("RSI High")

        if chg24 > 30:
            score += 25; reasons.append(f"+{chg24:.0f}% today EXTREME")
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

        if score >= 28:
            signal = "BEAR_REVERSAL"
            probability = min(30 + score, 88)
        else:
            return None
    else:
        return None

    if not signal:
        return None

    # Grade
    if probability >= 88:   grade = "S"
    elif probability >= 78: grade = "A"
    elif probability >= 68: grade = "B"
    elif probability >= 58: grade = "C"
    else:                   grade = "D"

    return {
        "symbol":        symbol,
        "probability":   probability,
        "signal":        signal,
        "grade":         grade,
        "price":         price,
        "rsi_daily":     rsi_daily,
        "change_24h":    chg24,
        "volume_24h_usd": vol24,
        "drop_from_high": drop_from_high,
        "rise_from_low":  rise_from_low,
        "signals_hit":    signals_hit,
        "reasons":        reasons[:3],
        "divergence":     "regular" if rsi_daily < 35 and chg24 > 0 else None,
        "choch_detected": False,
        "absorption":     False,
        "squeeze_score":  0,
        "timeframes_confirmed": [],
        "volume_ratio":   1.0,
        "source":         "python_scanner",
        "scanned_at":     datetime.datetime.now().isoformat(),
    }


def run_scan():
    """Full scan of top 200 coins. Called every 30 min."""
    logging.info("🔍 Starting Python reversal scan...")
    start = time.time()

    tickers = get_top_coins(200)
    if not tickers:
        logging.warning("No tickers from Binance — scan aborted")
        return

    bull_results = []
    bear_results = []
    scanned = 0
    errors  = 0

    for ticker in tickers:
        symbol = ticker.get("symbol","")
        if symbol in STABLES:
            continue
        try:
            # Get daily klines for RSI
            klines = get_klines(symbol, "1d", 30)
            result = score_coin(ticker, klines)
            scanned += 1

            if result:
                if result["signal"] == "BULL_REVERSAL":
                    bull_results.append(result)
                elif result["signal"] == "BEAR_REVERSAL":
                    bear_results.append(result)

            time.sleep(0.05)  # rate limit
        except Exception as e:
            errors += 1
            continue

    # Sort by probability
    bull_results.sort(key=lambda x: x["probability"], reverse=True)
    bear_results.sort(key=lambda x: x["probability"], reverse=True)

    # Top picks = S and A grade from both
    top = [r for r in bull_results + bear_results if r["grade"] in ("S","A")]
    top.sort(key=lambda x: x["probability"], reverse=True)

    elapsed = round(time.time() - start, 1)

    with scan_lock:
        latest_scan["bull_reversals"] = bull_results[:20]
        latest_scan["bear_reversals"] = bear_results[:20]
        latest_scan["top_picks"]      = top[:10]
        latest_scan["scanned_at"]     = datetime.datetime.now().isoformat()
        latest_scan["total_scanned"]  = scanned

    logging.info(
        f"✅ Scan done in {elapsed}s: {scanned} coins | "
        f"bull={len(bull_results)} bear={len(bear_results)} errors={errors}"
    )


def scan_scheduler():
    """Background thread — scans every 30 minutes."""
    while True:
        try:
            run_scan()
        except Exception as e:
            logging.error(f"Scan error: {e}")
        time.sleep(30 * 60)  # 30 minutes


# ── API routes ───────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":       "ok",
        "scanned_at":   latest_scan["scanned_at"],
        "bull_count":   len(latest_scan["bull_reversals"]),
        "bear_count":   len(latest_scan["bear_reversals"]),
        "browser_bull": len(browser_cache["bull"]),
        "browser_bear": len(browser_cache["bear"]),
    })


@app.route("/api/scan", methods=["GET"])
def api_scan():
    """Return the last full Python scan result."""
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
    """Trigger an immediate scan in background."""
    def _bg():
        try: run_scan()
        except Exception as e: logging.error(f"bg scan: {e}")
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "scan started"})


@app.route("/api/top", methods=["GET"])
def api_top():
    """Top picks (S + A grade). Falls back to browser cache."""
    with scan_lock:
        data = latest_scan["top_picks"]

    if not data:
        cached = browser_cache.get("top", [])
        if cached:
            logging.info(f"/api/top: serving {len(cached)} browser-cached")
            return jsonify(cached)
        # Also try bull+bear browser cache merged
        merged = browser_cache.get("bull", []) + browser_cache.get("bear", [])
        merged = [x for x in merged if x.get("grade","C") in ("S","A")]
        if merged:
            return jsonify(merged)

    return jsonify(data or [])


@app.route("/api/bull", methods=["GET"])
def api_bull():
    """Bull reversal signals. Falls back to browser cache."""
    with scan_lock:
        data = latest_scan["bull_reversals"]

    if not data:
        cached = browser_cache.get("bull", [])
        if cached:
            logging.info(f"/api/bull: serving {len(cached)} browser-cached")
            return jsonify(cached)

    return jsonify(data or [])


@app.route("/api/bear", methods=["GET"])
def api_bear():
    """Bear reversal signals. Falls back to browser cache."""
    with scan_lock:
        data = latest_scan["bear_reversals"]

    if not data:
        cached = browser_cache.get("bear", [])
        if cached:
            logging.info(f"/api/bear: serving {len(cached)} browser-cached")
            return jsonify(cached)

    return jsonify(data or [])


@app.route("/api/alert", methods=["GET", "POST"])
def api_alert():
    """
    Called by the dashboard JS when it detects a signal.
    Stores the signal in the browser cache.
    Also returns current alerts.
    """
    if request.method == "POST":
        try:
            data = request.get_json(force=True, silent=True) or {}
            symbol    = data.get("symbol", "")
            signal    = data.get("signal", data.get("type", ""))
            prob      = float(data.get("probability", data.get("prob", 50)))
            price     = float(data.get("price", 0))
            chg       = float(data.get("change", data.get("change_24h", 0)))
            source    = "browser_dashboard"

            if symbol and prob >= 45:
                if not symbol.endswith("USDT"):
                    symbol += "USDT"

                grade = "S" if prob >= 88 else "A" if prob >= 78 else "B" if prob >= 68 else "C"

                entry = {
                    "symbol":        symbol,
                    "probability":   prob,
                    "signal":        signal or ("BULL_REVERSAL" if chg < 0 else "BEAR_REVERSAL"),
                    "grade":         grade,
                    "price":         price,
                    "change_24h":    chg,
                    "volume_24h_usd": float(data.get("volume", 10_000_000)),
                    "rsi_daily":     float(data.get("rsi", 50)),
                    "signals_hit":   data.get("signals", []),
                    "reasons":       data.get("reasons", []),
                    "divergence":    data.get("divergence", None),
                    "choch_detected": data.get("choch", False),
                    "absorption":    data.get("absorption", False),
                    "squeeze_score": float(data.get("squeeze", 0)),
                    "timeframes_confirmed": data.get("timeframes", []),
                    "volume_ratio":  1.0,
                    "source":        source,
                    "detected_at":   datetime.datetime.now().isoformat(),
                }

                # Route to bull or bear cache
                sig_upper = entry["signal"].upper()
                if "BULL" in sig_upper:
                    # Deduplicate
                    browser_cache["bull"] = [
                        x for x in browser_cache["bull"] if x["symbol"] != symbol
                    ]
                    browser_cache["bull"].append(entry)
                    browser_cache["bull"].sort(key=lambda x: x["probability"], reverse=True)
                    browser_cache["bull"] = browser_cache["bull"][:30]
                elif "BEAR" in sig_upper:
                    browser_cache["bear"] = [
                        x for x in browser_cache["bear"] if x["symbol"] != symbol
                    ]
                    browser_cache["bear"].append(entry)
                    browser_cache["bear"].sort(key=lambda x: x["probability"], reverse=True)
                    browser_cache["bear"] = browser_cache["bear"][:30]

                if grade in ("S","A"):
                    browser_cache["top"] = [
                        x for x in browser_cache["top"] if x["symbol"] != symbol
                    ]
                    browser_cache["top"].append(entry)
                    browser_cache["top"].sort(key=lambda x: x["probability"], reverse=True)

                browser_cache["updated_at"] = datetime.datetime.now().isoformat()

                total = len(browser_cache["bull"]) + len(browser_cache["bear"])
                logging.info(f"📡 Alert: {symbol} {entry['signal']} {prob:.0f}% | cache: bull={len(browser_cache['bull'])} bear={len(browser_cache['bear'])}")

                return jsonify({
                    "status": "stored",
                    "symbol": symbol,
                    "grade":  grade,
                    "total_cached": total,
                })

        except Exception as e:
            logging.warning(f"alert POST error: {e}")

    # GET — return all current alerts
    all_alerts = browser_cache["bull"] + browser_cache["bear"]
    all_alerts.sort(key=lambda x: x["probability"], reverse=True)
    return jsonify(all_alerts)


@app.route("/api/signals/push", methods=["POST"])
def push_signals():
    """
    Dashboard JS posts bulk signals here every 60s.
    Payload: { bull: [...], bear: [...], top: [...] }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        updated = []

        now_iso = datetime.datetime.now().isoformat()

        for key in ("bull", "bear", "top", "candle_bull", "candle_bear"):
            if key in data and isinstance(data[key], list):
                # Normalize symbols
                normalized = []
                for item in data[key]:
                    if isinstance(item, dict) and item.get("symbol"):
                        sym = item["symbol"]
                        if not sym.endswith("USDT"):
                            sym += "USDT"
                        item["symbol"] = sym
                        item["source"] = "browser_dashboard"
                        item["detected_at"] = now_iso
                        # Ensure probability field
                        if "probability" not in item:
                            item["probability"] = float(
                                item.get("prob", item.get("reversal_probability", 50))
                            )
                        # Ensure grade
                        if "grade" not in item:
                            p = item["probability"]
                            item["grade"] = "S" if p>=88 else "A" if p>=78 else "B" if p>=68 else "C"
                        normalized.append(item)

                browser_cache[key] = normalized
                updated.append(f"{key}={len(normalized)}")

        browser_cache["updated_at"] = now_iso
        total = len(browser_cache["bull"]) + len(browser_cache["bear"])

        logging.info(f"📡 Signal push: {', '.join(updated)} | total={total}")

        return jsonify({
            "status": "ok",
            "received": updated,
            "total_cached": total,
            "updated_at": now_iso,
        })

    except Exception as e:
        logging.error(f"push_signals error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/signals/status", methods=["GET"])
def signals_status():
    """Quick check — is browser cache populated?"""
    return jsonify({
        "bull_count":  len(browser_cache["bull"]),
        "bear_count":  len(browser_cache["bear"]),
        "top_count":   len(browser_cache["top"]),
        "updated_at":  browser_cache["updated_at"],
        "scan_at":     latest_scan["scanned_at"],
        "python_bull": len(latest_scan["bull_reversals"]),
        "python_bear": len(latest_scan["bear_reversals"]),
        "top_3_bull":  browser_cache["bull"][:3],
        "top_3_bear":  browser_cache["bear"][:3],
    })


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "Reversal Bot Pro API",
        "endpoints": [
            "/api/bull", "/api/bear", "/api/top",
            "/api/scan", "/api/scan/now",
            "/api/alert", "/api/signals/push", "/api/signals/status",
            "/health"
        ]
    })


# ── Start ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Run Python scanner in background immediately + every 30 min
    scanner_thread = threading.Thread(target=scan_scheduler, daemon=True)
    scanner_thread.start()
    logging.info("🚀 Reversal Bot Pro server starting on port 5000")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
