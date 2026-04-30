"""
scanner_pro.py — Pro Multi-Timeframe Scanner
Uses 5m + 1H + 4H + 1D data + Funding Rate + Open Interest
"""

import requests
import time
import logging
from datetime import datetime

log = logging.getLogger(__name__)
BASE = "https://fapi.binance.com"


class ProScanner:

    def get_all_tickers(self):
        try:
            r = requests.get(f"{BASE}/fapi/v1/ticker/24hr", timeout=15)
            return [t for t in r.json() if t.get("symbol","").endswith("USDT")] if r.ok else []
        except Exception as e:
            log.error(f"Ticker error: {e}")
            return []

    def get_klines(self, symbol, interval, limit=100):
        try:
            r = requests.get(f"{BASE}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10)
            return r.json() if r.ok else []
        except:
            return []

    def get_funding_rate(self, symbol):
        try:
            r = requests.get(f"{BASE}/fapi/v1/fundingRate",
                params={"symbol": symbol, "limit": 1}, timeout=6)
            if r.ok and r.json():
                return float(r.json()[0].get("fundingRate", 0)) * 100
        except:
            pass
        return 0.0

    def get_open_interest_history(self, symbol):
        """Get OI now and 24h ago to detect OI changes."""
        try:
            r = requests.get(f"{BASE}/futures/data/openInterestHist",
                params={"symbol": symbol, "period": "1h", "limit": 25}, timeout=8)
            if r.ok and r.json():
                data = r.json()
                now  = float(data[-1].get("sumOpenInterestValue", 0))
                prev = float(data[0].get("sumOpenInterestValue", 0))
                return now, prev
        except:
            pass
        return 0, 0

    def get_order_book_ratio(self, symbol):
        """Get bid/ask ratio for order book imbalance."""
        try:
            r = requests.get(f"{BASE}/fapi/v1/depth",
                params={"symbol": symbol, "limit": 20}, timeout=8)
            if r.ok:
                d = r.json()
                bids = sum(float(p)*float(q) for p,q in d.get("bids",[]))
                asks = sum(float(p)*float(q) for p,q in d.get("asks",[]))
                return round(bids/asks, 2) if asks > 0 else 1.0
        except:
            pass
        return 1.0

    def full_scan(self, engine, news_scanner=None):
        """
        Full pro scan with all data sources.
        Returns ranked list sorted by probability.
        """
        tickers = self.get_all_tickers()
        if not tickers:
            return {"error": "No data", "scanned_at": datetime.utcnow().isoformat()}

        # Sort by volume — higher volume = more reliable signals
        tickers.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)

        log.info(f"Pro scanning {len(tickers)} coins...")

        results = []
        count   = 0

        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if not symbol:
                continue

            try:
                # Fetch all timeframes
                k1d = self.get_klines(symbol, "1d",  90)
                if not k1d or len(k1d) < 30:
                    continue

                k1h  = self.get_klines(symbol, "1h",  100)
                k4h  = self.get_klines(symbol, "4h",  60)
                k5m  = self.get_klines(symbol, "5m",  60)

                # Get funding + OI (only for coins with decent volume to save API calls)
                vol = float(ticker.get("quoteVolume", 0))
                funding = 0.0
                oi, prev_oi = 0, 0
                if vol > 1_000_000:  # $1M+ volume
                    funding = self.get_funding_rate(symbol)
                    oi, prev_oi = self.get_open_interest_history(symbol)
                    time.sleep(0.05)

                # Run analysis
                result = engine.analyze(
                    symbol, k5m, k1h, k4h, k1d,
                    ticker, funding, oi, prev_oi
                )

                if not result:
                    count += 1
                    continue

                # Add order book for top signals
                if result["probability"] >= 70:
                    ob_ratio = self.get_order_book_ratio(symbol)
                    result["order_book_ratio"] = ob_ratio
                    if ob_ratio > 1.5 and result["signal"] == "BULL_REVERSAL":
                        result["probability"] = min(result["probability"] + 5, 100)
                        result["reasons"].append(f"📗 Order book {ob_ratio}x more bids than asks")
                    elif ob_ratio < 0.67 and result["signal"] == "BEAR_REVERSAL":
                        result["probability"] = min(result["probability"] + 5, 100)
                        result["reasons"].append(f"📕 Order book {1/ob_ratio:.1f}x more asks than bids")

                # Add news
                if news_scanner and result["probability"] >= 65:
                    coin = symbol.replace("USDT", "")
                    news = news_scanner.get_news(coin)
                    if news:
                        result["news"] = news[:3]
                        sent = news_scanner.sentiment_score(news)
                        if sent > 0.3 and result["signal"] == "BULL_REVERSAL":
                            result["probability"] = min(result["probability"] + 8, 100)
                            result["reasons"].append(f"📰 Positive news sentiment backing the reversal")
                        elif sent < -0.3 and result["signal"] == "BEAR_REVERSAL":
                            result["probability"] = min(result["probability"] + 8, 100)
                            result["reasons"].append(f"📰 Negative news confirms bearish reversal")

                result["volume_24h_fmt"] = self._fmt(float(ticker.get("quoteVolume", 0)))
                result["funding_rate"]   = funding
                result["oi_current"]     = round(oi)
                result["oi_change_pct"]  = round((oi-prev_oi)/prev_oi*100, 1) if prev_oi > 0 else 0

                results.append(result)
                count += 1
                time.sleep(0.08)

            except Exception as e:
                log.debug(f"Error {symbol}: {e}")
                count += 1

        # RANK BY PROBABILITY
        results.sort(key=lambda x: x["probability"], reverse=True)

        # Split
        bull = [r for r in results if r["signal"] == "BULL_REVERSAL"]
        bear = [r for r in results if r["signal"] == "BEAR_REVERSAL"]

        # Market overview
        changes = [float(t.get("priceChangePercent", 0)) for t in tickers]
        up = sum(1 for c in changes if c > 0)
        down = len(changes) - up

        log.info(f"Pro scan done: {len(bull)} bull, {len(bear)} bear | Top: {results[0]['symbol'] if results else 'none'}")

        return {
            "scanned_at":    datetime.utcnow().isoformat(),
            "total_scanned": count,
            "bull_reversals": bull[:15],
            "bear_reversals": bear[:15],
            "top_picks":     results[:10],  # Absolute best regardless of direction
            "market": {
                "sentiment": "bullish" if up > down * 1.2 else "bearish" if down > up * 1.2 else "neutral",
                "up": up, "down": down,
                "avg_change": round(sum(changes)/len(changes), 2) if changes else 0,
            }
        }

    def _fmt(self, v):
        if v >= 1e9: return f"${v/1e9:.1f}B"
        if v >= 1e6: return f"${v/1e6:.1f}M"
        return f"${v/1e3:.0f}K"
