from flask import Flask, jsonify, Response
from reversal_pro import ProReversalEngine
from scanner_pro import ProScanner
from news import NewsScanner
from telegram_bot import TelegramAlerter
import threading, time, logging, os
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app      = Flask(__name__)
engine   = ProReversalEngine()
scanner  = ProScanner()
news_sc  = NewsScanner()
telegram = TelegramAlerter()

latest_scan    = {}
scan_history   = []
last_scan_time = None
is_scanning    = False
sent_alerts    = set()


def run_scan():
    global latest_scan, last_scan_time, is_scanning, scan_history
    if is_scanning:
        return
    is_scanning = True
    log.info("Starting Pro Reversal Scan...")
    try:
        result = scanner.full_scan(engine, news_sc)
        latest_scan    = result
        last_scan_time = datetime.utcnow()
        scan_history.append({
            "time": last_scan_time.isoformat(),
            "bull": len(result.get("bull_reversals", [])),
            "bear": len(result.get("bear_reversals", [])),
            "top":  result.get("top_picks", [{}])[0].get("symbol", "---") if result.get("top_picks") else "---",
        })
        if len(scan_history) > 20:
            scan_history.pop(0)
        _send_alerts(result)
    except Exception as e:
        log.error(f"Scan error: {e}")
    finally:
        is_scanning = False


def _send_alerts(result):
    global sent_alerts
    top  = result.get("top_picks", [])
    if not top and not result.get("bull_reversals") and not result.get("bear_reversals"):
        return
    telegram.send_scan_summary(result)
    for coin in top[:3]:
        sym = coin.get("symbol", "")
        key = "top_" + sym + "_" + datetime.utcnow().strftime("%Y%m%d%H")
        if coin["probability"] >= 75 and key not in sent_alerts:
            if coin["signal"] == "BULL_REVERSAL":
                telegram.send_bull_reversal(coin)
            else:
                telegram.send_bear_reversal(coin)
            sent_alerts.add(key)
            time.sleep(2)
    if len(sent_alerts) > 500:
        sent_alerts.clear()


def scheduler_loop():
    time.sleep(5)
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scheduler: {e}")
        time.sleep(1800)


@app.route("/")
def dashboard():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ["dashboard.html", os.path.join("templates","dashboard.html")]:
        path = os.path.join(here, name)
        if os.path.exists(path):
            with open(path, "r") as f:
                return Response(f.read(), mimetype="text/html")
    return Response(FALLBACK_HTML, mimetype="text/html")

@app.route("/api/scan")
def api_scan():
    return jsonify(latest_scan)

@app.route("/api/scan/now")
def api_scan_now():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/top")
def api_top():
    return jsonify(latest_scan.get("top_picks", []))

@app.route("/api/bull")
def api_bull():
    return jsonify(latest_scan.get("bull_reversals", []))

@app.route("/api/bear")
def api_bear():
    return jsonify(latest_scan.get("bear_reversals", []))

@app.route("/api/status")
def api_status():
    return jsonify({
        "scanning":   is_scanning,
        "last_scan":  last_scan_time.isoformat() if last_scan_time else None,
        "bull_count": len(latest_scan.get("bull_reversals", [])),
        "bear_count": len(latest_scan.get("bear_reversals", [])),
        "top_count":  len(latest_scan.get("top_picks", [])),
        "telegram":   telegram.enabled,
        "history":    scan_history,
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

FALLBACK_HTML = '<!DOCTYPE html><html><head><meta charset=UTF-8><title>Reversal Bot PRO</title>\n<style>body{background:#02050d;color:#00ff99;font-family:monospace;padding:40px;text-align:center}\nh1{font-size:2em;margin-bottom:20px}a{color:#00bbff}\n</style></head><body>\n<h1>REVERSAL BOT PRO</h1>\n<p style="color:#7799bb">Dashboard file missing — see fix below</p>\n<br>\n<a href="/api/scan">📊 View Scan Data (JSON)</a><br><br>\n<a href="/api/top">⭐ Top Picks (JSON)</a><br><br>\n<a href="/api/status">📡 Bot Status</a><br><br>\n<a href="/api/scan/now">⚡ Trigger Scan Now</a>\n</body></html>'

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"Reversal Bot Pro — port {port}")
    log.info(f"Telegram: {'ON' if telegram.enabled else 'OFF — add TELEGRAM_TOKEN + TELEGRAM_CHAT_1 + TELEGRAM_CHAT_2'}")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
