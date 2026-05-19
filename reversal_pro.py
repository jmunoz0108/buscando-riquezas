"""
reversal_pro.py — GAME CHANGER Reversal Detection Engine

FIXES applied:
- Bear reversal threshold: rise_from_low >= 20% → >= 8% (was missing coins near lows)
- Also triggers bear scan for coins pumped 15%+ TODAY (RONIN +33% = short target)
- Minimum probability: 38% → 28% (catches forming signals in bear market)
- Added "today's pump" bonus scoring for bear reversals
- Added D-grade tier for forming signals
"""

import numpy as np
import logging
from datetime import datetime

log = logging.getLogger(__name__)


class ProReversalEngine:

    def analyze(self, symbol, klines_5m, klines_1h, klines_4h, klines_1d, ticker, funding_rate=0, open_interest=0, prev_oi=0):
        try:
            if not klines_1d or len(klines_1d) < 30:
                return None

            d   = self._parse(klines_1d)
            h1  = self._parse(klines_1h)  if klines_1h  and len(klines_1h)  > 20 else None
            h4  = self._parse(klines_4h)  if klines_4h  and len(klines_4h)  > 10 else None
            m5  = self._parse(klines_5m)  if klines_5m  and len(klines_5m)  > 20 else None

            price   = float(ticker.get("lastPrice", d["close"][-1]))
            chg24   = float(ticker.get("priceChangePercent", 0))
            vol24   = float(ticker.get("quoteVolume", 0))
            vol_avg = float(np.mean([float(k[7]) for k in klines_1d[-14:]])) if len(klines_1d) >= 14 else vol24

            high52 = max(d["high"][-52:]) if len(d["high"]) >= 52 else max(d["high"])
            low52  = min(d["low"][-52:])  if len(d["low"])  >= 52 else min(d["low"])
            drop_from_high = (high52 - price) / high52 * 100
            rise_from_low  = (price - low52)  / low52  * 100

            rsi_d  = self._rsi(d["close"], 14)
            rsi_h1 = self._rsi(h1["close"], 14) if h1 else []
            rsi_h4 = self._rsi(h4["close"], 14) if h4 else []
            rsi_m5 = self._rsi(m5["close"], 14) if m5 else []

            macd_d = self._macd(d["close"])
            macd_h1= self._macd(h1["close"]) if h1 else None
            atr_d  = self._atr(d["high"], d["low"], d["close"], 14)

            # ── BULL REVERSAL ──
            if drop_from_high >= 10:
                bull = self._score_bull_reversal(
                    price, chg24, vol24, vol_avg, drop_from_high,
                    d, h1, h4, m5, rsi_d, rsi_h1, rsi_h4, rsi_m5,
                    macd_d, macd_h1, atr_d, funding_rate, open_interest, prev_oi
                )
                # Lowered from 38% to 28% — catches forming signals in bear market
                if bull["probability"] >= 28:
                    return self._build_result(symbol, price, chg24, vol24, vol_avg,
                                              drop_from_high, rise_from_low, rsi_d,
                                              "BULL_REVERSAL", bull, atr_d)

            # ── BEAR REVERSAL ──
            # FIX: was rise_from_low >= 20 — missed coins near yearly lows
            # Now: trigger if risen 8%+ from low OR pumped 15%+ today
            is_bear_candidate = rise_from_low >= 8 or chg24 >= 15
            if is_bear_candidate:
                bear = self._score_bear_reversal(
                    price, chg24, vol24, vol_avg, rise_from_low,
                    d, h1, h4, m5, rsi_d, rsi_h1, rsi_h4, rsi_m5,
                    macd_d, macd_h1, atr_d, funding_rate, open_interest, prev_oi
                )
                # Lowered from 38% to 28%
                if bear["probability"] >= 28:
                    return self._build_result(symbol, price, chg24, vol24, vol_avg,
                                              drop_from_high, rise_from_low, rsi_d,
                                              "BEAR_REVERSAL", bear, atr_d)

            return None

        except Exception as e:
            log.debug(f"Analysis error {symbol}: {e}")
            return None

    def _build_result(self, symbol, price, chg24, vol24, vol_avg,
                      drop, rise, rsi_d, signal, scores, atr_d):
        prob = scores["probability"]
        grade = (
            "S — PERFECT SETUP" if prob >= 88 else
            "A — STRONG SIGNAL" if prob >= 78 else
            "B — GOOD SIGNAL"   if prob >= 68 else
            "C — WATCH"         if prob >= 45 else
            "D — FORMING"
        )
        return {
            "symbol":          symbol,
            "price":           price,
            "change_24h":      round(chg24, 2),
            "volume_24h_usd":  round(vol24),
            "volume_ratio":    round(vol24 / vol_avg, 2) if vol_avg > 0 else 1.0,
            "drop_from_high":  round(drop, 1),
            "rise_from_low":   round(rise, 1),
            "rsi_daily":       round(rsi_d[-1], 1) if rsi_d else 50,
            "signal":          signal,
            "probability":     prob,
            "grade":           grade,
            "signals_hit":     scores["signals_hit"],
            "reasons":         scores["reasons"],
            "timeframes_confirmed": scores["timeframes"],
            "divergence":      scores.get("divergence", None),
            "squeeze_score":   scores.get("squeeze_score", 0),
            "choch_detected":  scores.get("choch", False),
            "absorption":      scores.get("absorption", False),
            "entry_zone":      scores.get("entry_zone"),
            "targets":         scores.get("targets"),
            "stop_loss":       scores.get("stop_loss"),
            "invalidation":    scores.get("invalidation"),
            "analyzed_at":     datetime.utcnow().isoformat(),
        }

    # ─────────────────────────────────────────────────
    #  BULL REVERSAL SCORING
    # ─────────────────────────────────────────────────

    def _score_bull_reversal(self, price, chg24, vol24, vol_avg, drop,
                              d, h1, h4, m5, rsi_d, rsi_h1, rsi_h4, rsi_m5,
                              macd_d, macd_h1, atr_d, funding_rate, oi, prev_oi):
        signals  = []
        reasons  = []
        timeframes = []
        points   = 0
        max_pts  = 0

        max_pts += 15
        div = self._detect_rsi_divergence(d["close"], d["low"], rsi_d, "bull")
        if div == "strong":
            points += 15
            signals.append("RSI Bullish Divergence (Strong)")
            reasons.append("🔥 STRONG RSI Divergence: price lower low, RSI higher low — hidden buying pressure")
            timeframes.append("1D")
        elif div == "regular":
            points += 10
            signals.append("RSI Bullish Divergence")
            reasons.append("📊 RSI Divergence: momentum turning before price — early reversal signal")
            timeframes.append("1D")

        div_h4 = self._detect_rsi_divergence(h4["close"], h4["low"], rsi_h4, "bull") if h4 else None
        if div_h4 in ("strong", "regular"):
            points += 5
            signals.append("RSI Divergence 4H")
            reasons.append("📊 4H RSI Divergence confirms daily — multi-timeframe alignment")
            if "4H" not in timeframes: timeframes.append("4H")

        max_pts += 15
        squeeze = self._squeeze_potential_bull(funding_rate, oi, prev_oi, chg24, vol24, vol_avg)
        if squeeze >= 80:
            points += 15
            signals.append("Short Squeeze Bomb")
            reasons.append(f"💣 SHORT SQUEEZE POTENTIAL {squeeze:.0f}%: Extreme negative funding ({funding_rate:.4f}%) + Rising OI = Forced liquidations incoming")
        elif squeeze >= 60:
            points += 10
            signals.append("Squeeze Setup")
            reasons.append(f"⚡ Squeeze setup {squeeze:.0f}%: Shorts heavily funded, price compressing")
        elif squeeze >= 40:
            points += 5
            signals.append("Mild Squeeze Pressure")
            reasons.append(f"🔺 Mild squeeze pressure — shorts uncomfortable")

        max_pts += 12
        choch = self._detect_choch_bull(d["high"], d["low"], d["close"])
        if choch == "confirmed":
            points += 12
            signals.append("CHoCH Confirmed (1D)")
            reasons.append("🏗️ CHANGE OF CHARACTER confirmed: First Higher Low after downtrend — Smart Money accumulating")
            if "1D" not in timeframes: timeframes.append("1D")

        choch_h4 = self._detect_choch_bull(h4["high"], h4["low"], h4["close"]) if h4 else None
        if choch_h4 == "confirmed":
            points += 5
            signals.append("CHoCH Confirmed (4H)")
            reasons.append("🏗️ 4H Change of Character — shorter timeframe confirms reversal")
            if "4H" not in timeframes: timeframes.append("4H")

        max_pts += 10
        absorption = self._detect_absorption(d["close"], d["open"], d["high"], d["low"], d["volume"], "bull")
        if absorption == "strong":
            points += 10
            signals.append("Volume Absorption (Strong)")
            reasons.append("🧲 STRONG ABSORPTION: Massive volume hitting the market but price barely moving — institutional buyers absorbing ALL sell pressure")
        elif absorption == "moderate":
            points += 6
            signals.append("Volume Absorption")
            reasons.append("🧲 Absorption detected: High volume, small bodies — buyers holding the line")

        max_pts += 10
        if macd_d and len(macd_d["hist"]) >= 5:
            h = macd_d["hist"]
            if h[-1] > h[-3] and h[-3] > h[-5] and d["close"][-1] < d["close"][-3]:
                points += 10
                signals.append("MACD Hidden Divergence")
                reasons.append("📈 MACD hidden bullish divergence: momentum accelerating while price still weak")
            elif h[-1] > h[-2] and h[-2] > h[-3] and h[-3] < 0:
                points += 7
                signals.append("MACD Turning Bullish")
                reasons.append("📈 MACD histogram reversing from negative — momentum shift confirmed")
            elif h[-1] > 0 and h[-2] <= 0:
                points += 5
                signals.append("MACD Zero Cross")
                reasons.append("📈 MACD crossed zero line bullish")

        max_pts += 8
        if rsi_d and len(rsi_d) >= 5:
            rsi_min = min(rsi_d[-10:]) if len(rsi_d) >= 10 else min(rsi_d)
            if rsi_min < 25 and rsi_d[-1] > rsi_d[-2]:
                points += 8
                signals.append("RSI Extreme Oversold Recovery")
                reasons.append(f"🔋 RSI hit extreme oversold ({rsi_min:.0f}) and now recovering — historically 80%+ reversal rate")
            elif rsi_d[-1] < 35 and rsi_d[-1] > rsi_d[-3]:
                points += 6
                signals.append("RSI Oversold Recovery")
                reasons.append(f"🔋 RSI oversold ({rsi_d[-1]:.0f}) turning up — buyers stepping in")
            elif rsi_d[-1] < 45 and rsi_d[-1] > rsi_d[-2]:
                points += 4
                signals.append("RSI Rising From Low")
                reasons.append(f"🔋 RSI ({rsi_d[-1]:.0f}) recovering — momentum building")
            elif rsi_d[-1] < 55 and rsi_d[-1] > rsi_d[-3]:
                points += 2
                reasons.append(f"📊 RSI ({rsi_d[-1]:.0f}) trending up")

        max_pts += 8
        tf_bull_count = 0
        if rsi_h1 and len(rsi_h1) >= 3 and rsi_h1[-1] > rsi_h1[-2] and rsi_h1[-1] < 50:
            tf_bull_count += 1
            if "1H" not in timeframes: timeframes.append("1H")
        if rsi_h4 and len(rsi_h4) >= 3 and rsi_h4[-1] > rsi_h4[-2] and rsi_h4[-1] < 55:
            tf_bull_count += 1
            if "4H" not in timeframes: timeframes.append("4H")
        if rsi_m5 and len(rsi_m5) >= 3 and rsi_m5[-1] > rsi_m5[-2] and rsi_m5[-1] > 40:
            tf_bull_count += 1
            if "5m" not in timeframes: timeframes.append("5m")
        if tf_bull_count >= 3:
            points += 8
            signals.append("Full TF Confluence (5m+1H+4H)")
            reasons.append(f"⚡ ALL timeframes aligned bullish — {tf_bull_count}/3 TF confirmation")
        elif tf_bull_count == 2:
            points += 4
            signals.append("TF Confluence (2/3)")
            reasons.append(f"📊 {tf_bull_count}/3 timeframes confirming bullish momentum")

        max_pts += 7
        vol_r = vol24 / vol_avg if vol_avg > 0 else 1
        if vol_r >= 3.0 and chg24 > 0:
            points += 7
            signals.append("Massive Buy Volume")
            reasons.append(f"💰 {vol_r:.1f}x normal volume on positive day — institutional accumulation")
        elif vol_r >= 2.0 and chg24 > 0:
            points += 6
            signals.append("Strong Buy Volume")
            reasons.append(f"💰 {vol_r:.1f}x volume surge with price recovery — genuine buying interest")
        elif vol_r >= 1.5:
            points += 4
            reasons.append(f"📊 Volume {vol_r:.1f}x above average — buying interest")
        elif vol_r >= 1.0:
            points += 2
            reasons.append(f"📊 Normal volume ({vol_r:.1f}x) — steady participation")

        if chg24 >= 8:
            points += 6
            reasons.append(f"📈 Strong +{chg24:.1f}% today — reversal momentum confirmed")
        elif chg24 >= 3:
            points += 4
            reasons.append(f"📈 Up {chg24:.1f}% today — recovery underway")
        elif chg24 >= 1:
            points += 2
            reasons.append(f"📈 Small gain today (+{chg24:.1f}%) — stabilizing")

        max_pts += 7
        if len(d["low"]) >= 10:
            recent_lows = d["low"][-10:]
            prev_lows   = d["low"][-20:-10] if len(d["low"]) >= 20 else recent_lows
            if min(recent_lows) > min(prev_lows) * 0.98:
                points += 7
                signals.append("Higher Lows Structure")
                reasons.append("📐 Higher lows forming — sellers losing strength, buyers defending higher levels")
            elif min(recent_lows[-3:]) > min(recent_lows[:3]):
                points += 4
                reasons.append("📐 Recent lows higher than earlier lows — slow accumulation forming")

        max_pts += 5
        if drop >= 70:
            points += 5
            reasons.append(f"💎 Fell {drop:.0f}% from high — historically deepest drops have most violent reversals")
        elif drop >= 50:
            points += 4
            reasons.append(f"📉 Down {drop:.0f}% from high — extreme oversold on macro scale")
        elif drop >= 30:
            points += 3
            reasons.append(f"📉 Down {drop:.0f}% from high — significant correction")
        elif drop >= 20:
            points += 2
            reasons.append(f"📉 Down {drop:.0f}% from high — pulled back from high")
        elif drop >= 10:
            points += 1
            reasons.append(f"📉 Down {drop:.0f}% from recent high")

        if funding_rate < -0.05:
            points += 3
            reasons.append(f"💸 Very negative funding ({funding_rate:.4f}%) — shorts paying heavily to stay short")

        if prev_oi > 0 and oi > prev_oi * 1.05 and chg24 < -2:
            points += 4
            reasons.append(f"📊 Open Interest rising while price falls — shorts stacking = squeeze fuel building")

        probability = round(min((points / max(max_pts, 1)) * 100, 100), 1) if max_pts > 0 else 0

        atr_val = atr_d[-1] if atr_d else price * 0.03
        t1 = round(price * 1.08, 6)
        t2 = round(price * 1.18, 6)
        t3 = round(price * 1.35, 6)
        sl = round(price - atr_val * 2, 6)

        return {
            "probability":   probability,
            "signals_hit":   signals,
            "reasons":       reasons,
            "timeframes":    list(set(timeframes)) or ["1D"],
            "divergence":    div if div else div_h4,
            "squeeze_score": squeeze,
            "choch":         choch == "confirmed",
            "absorption":    absorption in ("strong", "moderate"),
            "entry_zone":    f"${price:.6f} — ${price*1.015:.6f}",
            "targets":       {"T1": f"${t1}", "T2": f"${t2}", "T3": f"${t3}"},
            "stop_loss":     f"${sl}",
            "invalidation":  f"Close below ${round(sl * 0.98, 6)} on daily",
        }

    # ─────────────────────────────────────────────────
    #  BEAR REVERSAL SCORING
    # ─────────────────────────────────────────────────

    def _score_bear_reversal(self, price, chg24, vol24, vol_avg, rise,
                              d, h1, h4, m5, rsi_d, rsi_h1, rsi_h4, rsi_m5,
                              macd_d, macd_h1, atr_d, funding_rate, oi, prev_oi):
        signals  = []
        reasons  = []
        timeframes = []
        points   = 0
        max_pts  = 0

        max_pts += 15
        div = self._detect_rsi_divergence(d["close"], d["high"], rsi_d, "bear")
        if div == "strong":
            points += 15
            signals.append("RSI Bearish Divergence (Strong)")
            reasons.append("🔥 STRONG RSI Bearish Divergence: price higher high, RSI lower high — hidden selling")
            timeframes.append("1D")
        elif div == "regular":
            points += 10
            signals.append("RSI Bearish Divergence")
            reasons.append("📊 RSI Bearish Divergence: momentum fading while price still rising")

        max_pts += 15
        squeeze = self._squeeze_potential_bear(funding_rate, oi, prev_oi, chg24, vol24, vol_avg)
        if squeeze >= 80:
            points += 15
            signals.append("Long Squeeze Bomb")
            reasons.append(f"💣 LONG SQUEEZE POTENTIAL {squeeze:.0f}%: Extreme positive funding + Rising OI = Longs will be liquidated")
        elif squeeze >= 60:
            points += 10
            signals.append("Long Squeeze Setup")
            reasons.append(f"⚡ Long squeeze setup {squeeze:.0f}%: Longs paying to stay long, unsustainable")
        elif squeeze >= 40:
            points += 5
            reasons.append(f"🔻 Long squeeze pressure building")

        max_pts += 12
        choch = self._detect_choch_bear(d["high"], d["low"], d["close"])
        if choch == "confirmed":
            points += 12
            signals.append("Bear CHoCH Confirmed")
            reasons.append("🏗️ BEAR Change of Character: First Lower High after uptrend — Smart Money distributing")
            timeframes.append("1D")

        max_pts += 10
        absorption = self._detect_absorption(d["close"], d["open"], d["high"], d["low"], d["volume"], "bear")
        if absorption == "strong":
            points += 10
            signals.append("Distribution/Absorption")
            reasons.append("🧲 DISTRIBUTION detected: High volume but price not moving up — sellers absorbing all buying")
        elif absorption == "moderate":
            points += 6
            signals.append("Mild Distribution")
            reasons.append("🧲 Distribution pattern: weakening buying pressure")

        max_pts += 10
        if macd_d and len(macd_d["hist"]) >= 5:
            h = macd_d["hist"]
            if h[-1] < h[-3] < h[-5] and d["close"][-1] > d["close"][-3]:
                points += 10
                signals.append("MACD Hidden Bearish Divergence")
                reasons.append("📉 MACD hidden bearish divergence: momentum falling while price still rising")
            elif h[-1] < h[-2] < h[-3] and h[-3] > 0:
                points += 7
                signals.append("MACD Turning Bearish")
                reasons.append("📉 MACD histogram falling from positive — momentum reversing")

        max_pts += 8
        if rsi_d and len(rsi_d) >= 5:
            rsi_max = max(rsi_d[-10:]) if len(rsi_d) >= 10 else max(rsi_d)
            if rsi_max > 75 and rsi_d[-1] < rsi_d[-2]:
                points += 8
                signals.append("RSI Overbought Rejection")
                reasons.append(f"🔋 RSI hit extreme overbought ({rsi_max:.0f}) and reversing — historically 80%+ bear rate")
            elif rsi_d[-1] > 65 and rsi_d[-1] < rsi_d[-3]:
                points += 5
                signals.append("RSI Overbought Declining")
                reasons.append(f"🔋 RSI overbought ({rsi_d[-1]:.0f}) starting to fall")
            elif rsi_d[-1] > 60 and rsi_d[-1] < rsi_d[-2]:
                points += 3
                reasons.append(f"📊 RSI ({rsi_d[-1]:.0f}) extended and declining")

        max_pts += 8
        tf_bear_count = 0
        if rsi_h1 and len(rsi_h1) >= 3 and rsi_h1[-1] < rsi_h1[-2] and rsi_h1[-1] > 50:
            tf_bear_count += 1
            if "1H" not in timeframes: timeframes.append("1H")
        if rsi_h4 and len(rsi_h4) >= 3 and rsi_h4[-1] < rsi_h4[-2] and rsi_h4[-1] > 55:
            tf_bear_count += 1
            if "4H" not in timeframes: timeframes.append("4H")
        if rsi_m5 and len(rsi_m5) >= 3 and rsi_m5[-1] < rsi_m5[-2] and rsi_m5[-1] < 60:
            tf_bear_count += 1
            if "5m" not in timeframes: timeframes.append("5m")
        if tf_bear_count >= 3:
            points += 8
            signals.append("Full Bear TF Confluence")
            reasons.append(f"⚡ ALL timeframes aligned bearish — {tf_bear_count}/3 TF confirmation")
        elif tf_bear_count == 2:
            points += 4
            reasons.append(f"📊 {tf_bear_count}/3 timeframes bearish")

        max_pts += 7
        vol_r = vol24 / vol_avg if vol_avg > 0 else 1
        if vol_r < 0.5 and chg24 > 2:
            points += 7
            signals.append("Low Volume Rally")
            reasons.append(f"🚨 Only {vol_r:.1f}x normal volume on up day — weak rally, no conviction from buyers")
        elif vol_r < 0.75 and chg24 > 0:
            points += 4
            reasons.append(f"📊 Below average volume ({vol_r:.1f}x) — rally lacks conviction")

        max_pts += 7
        if len(d["high"]) >= 10:
            recent_highs = d["high"][-10:]
            prev_highs   = d["high"][-20:-10] if len(d["high"]) >= 20 else recent_highs
            if max(recent_highs) < max(prev_highs) * 1.02:
                points += 7
                signals.append("Lower Highs Structure")
                reasons.append("📐 Lower highs forming — buyers losing strength, distribution in progress")

        max_pts += 5
        if rise >= 150:
            points += 5
            reasons.append(f"🚀 Up {rise:.0f}% from low — parabolic moves always correct")
        elif rise >= 80:
            points += 4
            reasons.append(f"📈 Up {rise:.0f}% from low — extended, needs to cool off")
        elif rise >= 40:
            points += 2
            reasons.append(f"📈 Up {rise:.0f}% from low — notable extension")

        # ── TODAY'S PUMP BONUS (NEW) ──────────────────────────────────────────
        # Coins that pumped hard today are prime short targets
        max_pts += 10
        if chg24 >= 30:
            points += 10
            signals.append("Massive Single-Day Pump")
            reasons.append(f"🚨 UP {chg24:.0f}% TODAY — parabolic single-day moves always retrace 50-80%")
        elif chg24 >= 20:
            points += 8
            signals.append("Large Single-Day Pump")
            reasons.append(f"🚨 UP {chg24:.0f}% today — extreme single-day move, reversal likely")
        elif chg24 >= 15:
            points += 6
            signals.append("Significant Daily Pump")
            reasons.append(f"📈 UP {chg24:.0f}% today — overextended, watching for reversal")
        elif chg24 >= 10:
            points += 4
            reasons.append(f"📈 UP {chg24:.0f}% today — notable daily move")

        # RSI overbought after big pump = double signal
        if rsi_d and rsi_d[-1] > 70 and chg24 >= 10:
            points += 6
            signals.append("RSI Overbought After Pump")
            reasons.append(f"⚠️  RSI {rsi_d[-1]:.0f} overbought after {chg24:.0f}% pump — double reversal signal")

        if funding_rate > 0.05:
            points += 3
            reasons.append(f"💸 Very positive funding ({funding_rate:.4f}%) — longs paying, unsustainable")

        if prev_oi > 0 and oi > prev_oi * 1.05 and chg24 > 2:
            points += 4
            reasons.append(f"📊 OI rising while price pumps — overleveraged longs = liquidation risk")

        probability = round(min((points / max(max_pts, 1)) * 100, 100), 1) if max_pts > 0 else 0

        atr_val = atr_d[-1] if atr_d else price * 0.03
        t1 = round(price * 0.92, 6)
        t2 = round(price * 0.82, 6)
        t3 = round(price * 0.70, 6)
        sl = round(price + atr_val * 2, 6)

        return {
            "probability":   probability,
            "signals_hit":   signals,
            "reasons":       reasons,
            "timeframes":    list(set(timeframes)) or ["1D"],
            "divergence":    div,
            "squeeze_score": squeeze,
            "choch":         choch == "confirmed",
            "absorption":    absorption in ("strong", "moderate"),
            "entry_zone":    f"${price*0.99:.6f} — ${price:.6f}",
            "targets":       {"T1": f"${t1}", "T2": f"${t2}", "T3": f"${t3}"},
            "stop_loss":     f"${sl}",
            "invalidation":  f"Close above ${round(sl * 1.02, 6)} on daily",
        }

    # ─────────────────────────────────────────────────
    #  DETECTION METHODS
    # ─────────────────────────────────────────────────

    def _detect_rsi_divergence(self, closes, pivots, rsi_vals, direction):
        if len(rsi_vals) < 10 or len(closes) < 10:
            return None

        closes  = np.array(closes)
        pivots  = np.array(pivots)
        rsi_arr = np.array(rsi_vals)

        if direction == "bull":
            p1_idx = len(pivots) - 1
            p2_idx = max(0, len(pivots) - 8)
            p1_price = min(pivots[p2_idx:])
            p2_price = min(pivots[max(0, p2_idx-8):p2_idx]) if p2_idx > 0 else p1_price
            rsi_at_p1 = min(rsi_arr[max(0, p2_idx-2):])
            rsi_at_p2 = min(rsi_arr[:max(1, p2_idx-6)]) if p2_idx > 6 else rsi_at_p1
            if p1_price < p2_price * 0.97 and rsi_at_p1 > rsi_at_p2 + 3:
                if rsi_at_p1 < 40:
                    return "strong" if (rsi_at_p1 - rsi_at_p2) > 8 else "regular"
        else:
            p2_idx = max(0, len(pivots) - 8)
            p1_price = max(pivots[p2_idx:])
            p2_price = max(pivots[:max(1, p2_idx)]) if p2_idx > 0 else p1_price
            rsi_at_p1 = max(rsi_arr[max(0, p2_idx-2):])
            rsi_at_p2 = max(rsi_arr[:max(1, p2_idx-6)]) if p2_idx > 6 else rsi_at_p1
            if p1_price > p2_price * 1.03 and rsi_at_p1 < rsi_at_p2 - 3:
                if rsi_at_p1 > 60:
                    return "strong" if (rsi_at_p2 - rsi_at_p1) > 8 else "regular"

        return None

    def _squeeze_potential_bull(self, funding_rate, oi, prev_oi, chg24, vol24, vol_avg):
        score = 0
        if funding_rate < -0.10:  score += 40
        elif funding_rate < -0.05: score += 28
        elif funding_rate < -0.02: score += 15
        elif funding_rate < -0.01: score += 8
        if prev_oi > 0:
            oi_change = (oi - prev_oi) / prev_oi * 100
            if oi_change > 15 and chg24 < -5:  score += 30
            elif oi_change > 10 and chg24 < -3: score += 20
            elif oi_change > 5 and chg24 < 0:  score += 10
        if chg24 > 5:   score += 20
        elif chg24 > 2: score += 12
        elif chg24 > 0: score += 5
        vol_r = vol24 / vol_avg if vol_avg > 0 else 1
        if vol_r > 3:   score += 10
        elif vol_r > 2: score += 6
        return min(score, 100)

    def _squeeze_potential_bear(self, funding_rate, oi, prev_oi, chg24, vol24, vol_avg):
        score = 0
        if funding_rate > 0.10:   score += 40
        elif funding_rate > 0.05: score += 28
        elif funding_rate > 0.02: score += 15
        elif funding_rate > 0.01: score += 8
        if prev_oi > 0:
            oi_change = (oi - prev_oi) / prev_oi * 100
            if oi_change > 15 and chg24 > 5:   score += 30
            elif oi_change > 10 and chg24 > 2: score += 20
            elif oi_change > 5 and chg24 > 0:  score += 10
        if chg24 < -5:   score += 20
        elif chg24 < -2: score += 12
        elif chg24 < 0:  score += 5
        vol_r = vol24 / vol_avg if vol_avg > 0 else 1
        if vol_r > 3:   score += 10
        elif vol_r > 2: score += 6
        return min(score, 100)

    def _detect_choch_bull(self, highs, lows, closes):
        if len(lows) < 8:
            return None
        lows = np.array(lows)
        highs = np.array(highs)
        recent = lows[-8:]
        had_lower_lows = recent[2] < recent[0] and recent[4] < recent[2]
        if had_lower_lows and len(recent) >= 7:
            last_low  = recent[-1]
            prev_low  = recent[-3]
            if last_low > prev_low * 1.005:
                if closes[-1] > (highs[-1] + lows[-1]) / 2:
                    return "confirmed"
                return "forming"
        return None

    def _detect_choch_bear(self, highs, lows, closes):
        if len(highs) < 8:
            return None
        highs = np.array(highs)
        recent = highs[-8:]
        had_higher_highs = recent[2] > recent[0] and recent[4] > recent[2]
        if had_higher_highs and len(recent) >= 7:
            last_high = recent[-1]
            prev_high = recent[-3]
            if last_high < prev_high * 0.995:
                if closes[-1] < (highs[-1] + lows[-1]) / 2:
                    return "confirmed"
                return "forming"
        return None

    def _detect_absorption(self, closes, opens, highs, lows, volumes, direction):
        if len(volumes) < 10:
            return None
        avg_vol  = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        avg_body = np.mean([abs(closes[i] - opens[i]) for i in range(-10, 0)])
        absorption_count = 0
        for i in range(-5, 0):
            body  = abs(closes[i] - opens[i])
            range_ = highs[i] - lows[i]
            vol   = volumes[i]
            if vol > avg_vol * 1.8 and range_ > 0 and body / range_ < 0.25:
                if direction == "bull":
                    if closes[i] > (highs[i] + lows[i]) / 2:
                        absorption_count += 1
                else:
                    if closes[i] < (highs[i] + lows[i]) / 2:
                        absorption_count += 1
        if absorption_count >= 3:
            return "strong"
        elif absorption_count >= 2:
            return "moderate"
        return None

    def _parse(self, klines):
        return {
            "open":   [float(k[1]) for k in klines],
            "high":   [float(k[2]) for k in klines],
            "low":    [float(k[3]) for k in klines],
            "close":  [float(k[4]) for k in klines],
            "volume": [float(k[5]) for k in klines],
        }

    def _rsi(self, closes, period=14):
        if len(closes) < period + 2:
            return []
        c = np.array(closes, dtype=float)
        delta = np.diff(c)
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_g = np.mean(gains[:period])
        avg_l = np.mean(losses[:period])
        rsi = []
        for i in range(period, len(delta)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            rs = avg_g / avg_l if avg_l > 0 else 100
            rsi.append(round(100 - 100 / (1 + rs), 2))
        return rsi

    def _macd(self, closes, fast=12, slow=26, signal=9):
        if len(closes) < slow + signal + 2:
            return None
        c = np.array(closes, dtype=float)
        def ema(data, n):
            e = [data[0]]
            k = 2 / (n + 1)
            for v in data[1:]:
                e.append(v * k + e[-1] * (1 - k))
            return np.array(e)
        ef = ema(c, fast)
        es = ema(c, slow)
        ml = ef - es
        sl2 = ema(ml, signal)
        return {"macd": ml, "signal": sl2, "hist": ml - sl2}

    def _atr(self, highs, lows, closes, period=14):
        if len(closes) < period + 2:
            return []
        tr = []
        for i in range(1, len(closes)):
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            ))
        atr = [np.mean(tr[:period])]
        for t in tr[period:]:
            atr.append((atr[-1] * (period - 1) + t) / period)
        return atr
