"""Compare MACD line/signal position with an actual completed-bar crossover.

Standard 4h/day MACD: 12,26,9. Chart-compatible daily variant: 3,10,16.
Cross event refers ONLY to the latest completed candle. Bullish status does
not imply that a new golden cross happened on that candle.
"""
from datetime import timedelta
from btc_mtf_rebound_v12 import ema_series


def analyze_macd(rows, fast, slow, signal, tf, bar_end, contiguous):
    minimum = slow + signal + 10
    empty = {
        "state": "UNKNOWN", "event": "NONE", "last_event": "NONE",
        "last_event_kst": None, "bars_since_cross": None,
        "macd": None, "signal": None, "hist": None, "prev_hist": None,
        "continuous": False, "params": f"{fast},{slow},{signal}",
        "tf": tf, "closed_kst": None,
    }
    if not (0 < fast < slow and signal > 0 and len(rows) >= minimum):
        return empty
    prices = [r["c"] for r in rows]
    efast = ema_series(prices, fast)
    eslow = ema_series(prices, slow)
    line = [x-y for x, y in zip(efast, eslow)]
    sig = ema_series(line, signal)
    hist = [x-y for x, y in zip(line, sig)]
    recent_ok = contiguous(rows, tf, 3)
    current = hist[-1]
    state = "GOLDEN" if current > 0 else "DEAD" if current < 0 else "FLAT"
    if not recent_ok:
        state = "UNKNOWN"
    def cross(previous, latest):
        if previous <= 0 < latest:
            return "GOLDEN_CROSS"
        if previous >= 0 > latest:
            return "DEAD_CROSS"
        return "NONE"
    event = cross(hist[-2], hist[-1]) if recent_ok else "NONE"
    last_event = "NONE"
    last_at = None
    bars_ago = None
    # Search last 24 completed candles, but never claim a crossover across
    # an interval for which a real bar is missing.
    step = timedelta(hours=4) if tf == "4h" else timedelta(days=1)
    for i in range(len(rows)-1, max(0, len(rows)-25), -1):
        if rows[i]["t"] - rows[i-1]["t"] != step:
            continue
        kind = cross(hist[i-1], hist[i])
        if kind != "NONE":
            last_event = kind
            last_at = bar_end(rows[i]["t"], tf).strftime("%Y-%m-%d %H:%M")
            bars_ago = len(rows)-1-i
            break
    return {
        "state": state, "event": event,
        "last_event": last_event, "last_event_kst": last_at,
        "bars_since_cross": bars_ago, "continuous": recent_ok,
        "macd": round(line[-1], 10), "signal": round(sig[-1], 10),
        "hist": round(current, 10), "prev_hist": round(hist[-2], 10),
        "params": f"{fast},{slow},{signal}", "tf": tf,
        "closed_kst": bar_end(rows[-1]["t"], tf).strftime("%Y-%m-%d %H:%M"),
    }


def all_macd_views(day, h4, bar_end, contiguous):
    return {
        "h4": analyze_macd(h4, 12, 26, 9, "4h", bar_end, contiguous),
        "day": analyze_macd(day, 12, 26, 9, "day", bar_end, contiguous),
        "day_chart": analyze_macd(day, 3, 10, 16, "day", bar_end, contiguous),
    }
