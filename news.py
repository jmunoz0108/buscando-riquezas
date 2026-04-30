"""
news.py — Crypto News + Social Sentiment Scanner
Uses CryptoPanic (free, no key needed for basic) + RSS feeds
Searches for news/discussion about specific coins
"""

import requests
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Optional: set CRYPTOPANIC_KEY in Railway env vars for more results
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"


class NewsScanner:

    def get_news(self, coin_symbol, limit=5):
        """
        Fetch recent news for a coin.
        Returns list of news items with title, url, sentiment, time.
        """
        results = []

        # 1. CryptoPanic API
        cp_news = self._cryptopanic(coin_symbol, limit)
        results.extend(cp_news)

        # 2. Binance announcement RSS
        if not results:
            binance_news = self._binance_announcements(coin_symbol)
            results.extend(binance_news)

        return results[:limit]

    def _cryptopanic(self, coin, limit=5):
        """CryptoPanic free API — best crypto news aggregator."""
        try:
            params = {
                "auth_token": CRYPTOPANIC_KEY if CRYPTOPANIC_KEY else "anonymous",
                "currencies":  coin.upper(),
                "public":      "true",
                "kind":        "news",
            }
            r = requests.get(CRYPTOPANIC_URL, params=params, timeout=10)
            if not r.ok:
                return []

            data = r.json()
            results = []
            cutoff = datetime.utcnow() - timedelta(hours=48)

            for item in data.get("results", [])[:limit]:
                pub = item.get("published_at", "")
                try:
                    pub_dt = datetime.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S")
                    if pub_dt < cutoff:
                        continue
                except:
                    pass

                votes = item.get("votes", {})
                positive = votes.get("positive", 0)
                negative = votes.get("negative", 0)
                total_votes = positive + negative

                if total_votes > 0:
                    sentiment = (positive - negative) / total_votes
                else:
                    sentiment = 0.0

                results.append({
                    "title":     item.get("title", ""),
                    "url":       item.get("url", ""),
                    "source":    item.get("source", {}).get("title", ""),
                    "sentiment": round(sentiment, 2),
                    "published": pub[:16].replace("T", " "),
                    "votes":     total_votes,
                })

            return results

        except Exception as e:
            log.debug(f"CryptoPanic error for {coin}: {e}")
            return []

    def _binance_announcements(self, coin):
        """Check Binance announcements for new listings or delistings."""
        try:
            r = requests.get(
                "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query",
                params={"catalogId": "48", "pageNo": 1, "pageSize": 20},
                timeout=10
            )
            if not r.ok:
                return []

            data = r.json()
            articles = data.get("data", {}).get("articles", [])
            results = []
            coin_upper = coin.upper()

            for article in articles:
                title = article.get("title", "")
                if coin_upper in title.upper():
                    results.append({
                        "title":     title,
                        "url":       f"https://www.binance.com/en/support/announcement/{article.get('code','')}",
                        "source":    "Binance",
                        "sentiment": 0.3 if "list" in title.lower() else -0.3 if "delist" in title.lower() else 0.0,
                        "published": "",
                        "votes":     0,
                    })

            return results

        except Exception as e:
            log.debug(f"Binance announcement error: {e}")
            return []

    def sentiment_score(self, news_items):
        """
        Calculate overall sentiment from a list of news items.
        Returns -1.0 (very negative) to +1.0 (very positive)
        """
        if not news_items:
            return 0.0

        weighted_sum = 0.0
        weight_total = 0.0

        for item in news_items:
            s = item.get("sentiment", 0.0)
            v = max(item.get("votes", 1), 1)
            weighted_sum += s * v
            weight_total += v

        if weight_total == 0:
            return 0.0

        return round(weighted_sum / weight_total, 3)

    def get_fear_greed(self):
        """Get current Fear & Greed index."""
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            if r.ok:
                data = r.json()["data"][0]
                return {
                    "value":       int(data["value"]),
                    "label":       data["value_classification"],
                    "updated":     data.get("timestamp", ""),
                }
        except Exception as e:
            log.debug(f"Fear&Greed error: {e}")
        return {"value": 50, "label": "Neutral", "updated": ""}
