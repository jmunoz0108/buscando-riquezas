"""
telegram_bot.py — Dual Telegram Alert System
Sends formatted reversal alerts to 2 chat IDs
"""

import requests
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_1 = os.getenv("TELEGRAM_CHAT_1", "")
TELEGRAM_CHAT_2 = os.getenv("TELEGRAM_CHAT_2", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


class TelegramAlerter:

    def __init__(self):
        self.chat_ids = [c for c in [TELEGRAM_CHAT_1, TELEGRAM_CHAT_2] if c]
        self.enabled  = bool(TELEGRAM_TOKEN and self.chat_ids)
        if not self.enabled:
            log.warning("Telegram not configured — set TELEGRAM_TOKEN, TELEGRAM_CHAT_1, TELEGRAM_CHAT_2")

    def send_bull_reversal(self, coin):
        """Send bullish reversal alert."""
        sym  = coin["symbol"].replace("USDT", "")
        conf = coin["confidence"]
        stars = "⭐" * (1 if conf < 65 else 2 if conf < 80 else 3)

        msg = (
            f"🟢 *BULL REVERSAL DETECTED* {stars}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 *{sym}/USDT*  |  `${coin['price']}`\n"
            f"📊 Confidence: *{conf}%*\n"
            f"📉 Dropped: *{coin.get('drop_from_high',0):.0f}%* from high\n"
            f"📈 Today: *+{coin.get('change_24h',0):.1f}%*\n"
            f"📦 Volume: *{coin.get('volume_24h','—')}*\n"
            f"💪 Buy Pressure: *{coin.get('buy_pressure',50):.0f}%*\n"
            f"⏱ Timeframes: {', '.join(coin.get('timeframes',['1D'])) or '1D'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry Zone: `{coin.get('entry_zone','—')}`\n"
            f"🎯 Targets: `{coin.get('target','—')}`\n"
            f"🛑 Stop Loss: `{coin.get('stop','—')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *Reasons:*\n"
        )

        for r in coin.get("reasons", [])[:5]:
            msg += f"  • {r}\n"

        # Add news if any
        news = coin.get("news", [])
        if news:
            msg += f"\n📰 *Recent News:*\n"
            for n in news[:2]:
                msg += f"  • [{n['title'][:60]}...]({n['url']})\n"

        # Funding rate info
        fr = coin.get("funding_rate", 0)
        if abs(fr) > 0.01:
            msg += f"\n💰 Funding Rate: *{fr:.4f}%*"
            if fr < -0.01:
                msg += " _(negative = shorts paying, bullish)_"

        # Order book
        ob = coin.get("order_book")
        if ob:
            msg += f"\n📗 Order Book: *{ob['ratio']}x* buy/sell ratio"
            if ob.get("big_bids"):
                top_bid = ob["big_bids"][0]
                msg += f"\n💚 Big buy wall: ${top_bid['price']} (${top_bid['qty_usd']:,})"

        msg += f"\n\n🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        msg += f"\n📊 [View Chart](https://www.tradingview.com/chart/?symbol=BINANCE:{sym}USDT.P)"

        self._send(msg)

    def send_bear_reversal(self, coin):
        """Send bearish reversal alert."""
        sym  = coin["symbol"].replace("USDT", "")
        conf = coin["confidence"]
        stars = "⭐" * (1 if conf < 65 else 2 if conf < 80 else 3)

        msg = (
            f"🔴 *BEAR REVERSAL DETECTED* {stars}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *{sym}/USDT*  |  `${coin['price']}`\n"
            f"📊 Confidence: *{conf}%*\n"
            f"📈 Pumped: *+{coin.get('rise_from_low',0):.0f}%* from low\n"
            f"📉 Today: *{coin.get('change_24h',0):.1f}%*\n"
            f"📦 Volume: *{coin.get('volume_24h','—')}*\n"
            f"🐻 Sell Pressure: *{100 - coin.get('buy_pressure',50):.0f}%*\n"
            f"⏱ Timeframes: {', '.join(coin.get('timeframes',['1D'])) or '1D'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry Zone: `{coin.get('entry_zone','—')}`\n"
            f"🎯 Targets: `{coin.get('target','—')}`\n"
            f"🛑 Stop Loss: `{coin.get('stop','—')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *Reasons:*\n"
        )

        for r in coin.get("reasons", [])[:5]:
            msg += f"  • {r}\n"

        news = coin.get("news", [])
        if news:
            msg += f"\n📰 *Recent News:*\n"
            for n in news[:2]:
                msg += f"  • [{n['title'][:60]}...]({n['url']})\n"

        fr = coin.get("funding_rate", 0)
        if fr > 0.01:
            msg += f"\n💰 Funding Rate: *+{fr:.4f}%* _(longs paying, bearish)_"

        ob = coin.get("order_book")
        if ob:
            msg += f"\n📕 Order Book: *{ob['ratio']}x* buy/sell ratio"
            if ob.get("big_asks"):
                top_ask = ob["big_asks"][0]
                msg += f"\n❤️ Big sell wall: ${top_ask['price']} (${top_ask['qty_usd']:,})"

        msg += f"\n\n🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        msg += f"\n📊 [View Chart](https://www.tradingview.com/chart/?symbol=BINANCE:{sym}USDT.P)"

        self._send(msg)

    def send_scan_summary(self, results):
        """Send a quick scan summary to Telegram."""
        bull = results.get("bull_reversals", [])
        bear = results.get("bear_reversals", [])
        mkt  = results.get("market", {})

        if not bull and not bear:
            return

        sent = mkt.get("sentiment", "neutral").upper()
        emoji = "🟢" if sent == "BULLISH" else "🔴" if sent == "BEARISH" else "🟡"

        msg = (
            f"🔍 *REVERSAL SCAN COMPLETE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} Market: *{sent}* | Avg: *{mkt.get('avg_change',0):+.1f}%*\n"
            f"🟢 Up: {mkt.get('up',0)} | 🔴 Down: {mkt.get('down',0)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Bull Reversals Found: *{len(bull)}*\n"
        )

        for c in bull[:5]:
            sym = c["symbol"].replace("USDT","")
            msg += f"  ✅ {sym} — {c['confidence']}% conf | {c.get('change_24h',0):+.1f}% today\n"

        msg += f"\n🔴 Bear Reversals Found: *{len(bear)}*\n"
        for c in bear[:5]:
            sym = c["symbol"].replace("USDT","")
            msg += f"  ⛔ {sym} — {c['confidence']}% conf | {c.get('change_24h',0):+.1f}% today\n"

        msg += f"\n🕐 {datetime.utcnow().strftime('%H:%M')} UTC"

        self._send(msg)

    def send_message(self, text):
        """Send a plain text message."""
        self._send(text)

    def _send(self, text):
        """Send message to all configured chat IDs."""
        if not self.enabled:
            log.info(f"Telegram (disabled): {text[:100]}")
            return

        for chat_id in self.chat_ids:
            try:
                r = requests.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={
                        "chat_id":    chat_id,
                        "text":       text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": False,
                    },
                    timeout=10
                )
                if not r.ok:
                    log.error(f"Telegram send failed to {chat_id}: {r.text[:200]}")
            except Exception as e:
                log.error(f"Telegram error: {e}")
