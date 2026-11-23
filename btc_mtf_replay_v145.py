"""BTC MTF V1.4.5: bounded, auditable candle recovery for 120h replay.

No synthetic prices are created. The replay is based only on real Bithumb
1h OHLC candles, with exact KST timestamps. A 30m query is a diagnostic
only and NEVER supplies a missing 1h price for virtual exit execution.
"""
from datetime import timedelta

AUDIT_RULE = "REPLAY_V145"
LIMIT_HOURS = 120


def _clock(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


class HourlyReplayPath:
    def __init__(self, market, first_hour, getter, detailed=False, limit=LIMIT_HOURS):
        self.market = market
        self.first_hour = first_hour
        self.end_hour = first_hour + timedelta(hours=limit)
        self.getter = getter
        self.detailed = bool(detailed)
        self.index = {}
        self.api_calls = 0
        self.hourly_rechecks = 0
        self.half_hour_diagnostics = 0
        self.recovered = 0
        self.initial_error = ""
        self.initial_missing_count = None
        self.initial_missing_samples = []
        self.last_gap_at = None
        self.last_gap_status = "NONE"
        self.last_lower_tf_count = None
        self.tried_times = set()
        self.max_hourly_rechecks = 2
        self._initial_load()

    def _call(self, tf, cursor, count):
        self.api_calls += 1
        return self.getter(self.market, tf, cursor, count=count)

    def _merge(self, rows):
        found = 0
        for row in rows:
            if not isinstance(row, dict) or "t" not in row:
                continue
            t = row["t"]
            if not self.first_hour <= t < self.end_hour:
                continue
            # Never overwrite a verified first occurrence with a conflicting
            # response from another request.
            if t not in self.index:
                self.index[t] = row
                found += 1
        return found

    def _initial_load(self):
        # Count=160 is preserved from V1.4.4, so the primary baseline is
        # directly comparable. "to" is KST and exclusive in Bithumb docs.
        cursor = self.end_hour + timedelta(minutes=1)
        try:
            rows = self._call("1h", cursor, 160)
            self._merge(rows)
        except Exception as ex:
            self.initial_error = str(ex)[:120]
        missing = [
            self.first_hour + timedelta(hours=i)
            for i in range(LIMIT_HOURS)
            if self.first_hour + timedelta(hours=i) not in self.index
        ]
        self.initial_missing_count = len(missing)
        self.initial_missing_samples = [_clock(x) for x in missing[:5]]

    def hour(self, stamp):
        """Return verified 1h candle, or None if source cannot confirm it."""
        row = self.index.get(stamp)
        if row is not None:
            return row
        self.last_gap_at = stamp
        if stamp in self.tried_times:
            return None
        self.tried_times.add(stamp)
        if self.hourly_rechecks >= self.max_hourly_rechecks:
            self.last_gap_status = "RECHECK_BUDGET_EXHAUSTED"
            return None

        # A small exact time-bounded request avoids relying on the ranking
        # of sparse candles from a single large historical batch.
        self.hourly_rechecks += 1
        try:
            fresh = self._call("1h", stamp + timedelta(hours=1, minutes=1), 8)
            self._merge(fresh)
        except Exception as ex:
            self.last_gap_status = "HOURLY_RECHECK_API_ERROR"
            self.last_error = str(ex)[:120]
            return None
        row = self.index.get(stamp)
        if row is not None:
            self.recovered += 1
            self.last_gap_status = "RECOVERED_1H"
            return row

        self.last_gap_status = "SOURCE_1H_NOT_RETURNED"
        if not self.detailed:
            return None

        # A 30m source check can help distinguish a suspicious missing 1h
        # candle from a likely low-activity period. Never aggregate 30m
        # into 1h OHLC or assume no trade occurred solely from this response.
        self.half_hour_diagnostics += 1
        try:
            rows30 = self._call("30m", stamp + timedelta(hours=1, minutes=1), 8)
            self.last_lower_tf_count = sum(
                1 for r in rows30
                if isinstance(r, dict) and "t" in r
                and stamp <= r["t"] < stamp + timedelta(hours=1)
            )
            if self.last_lower_tf_count:
                self.last_gap_status = "SOURCE_1H_MISSING_BUT_30M_EXISTS"
            else:
                self.last_gap_status = "SOURCE_1H_30M_NOT_RETURNED"
        except Exception as ex:
            self.last_gap_status = "SOURCE_1H_MISSING_30M_API_ERROR"
            self.last_error = str(ex)[:120]
        return None

    def audit(self):
        code = self.last_gap_status
        if code == "NONE":
            code = ("INITIAL_API_ERROR" if self.initial_error else
                    "COMPLETE_1H_PATH" if self.initial_missing_count == 0
                    else "NO_RECHECK_NEEDED_BEFORE_EXIT")
        return {
            "rule": AUDIT_RULE,
            "status_code": code,
            "initial_api_error": self.initial_error,
            "detail_error": getattr(self, "last_error", ""),
            "initial_missing_hours": self.initial_missing_count,
            "initial_missing_samples_kst": list(self.initial_missing_samples),
            "verified_hourly_candles": len(self.index),
            "first_unresolved_or_recovered_hour_kst": _clock(self.last_gap_at),
            "hourly_recheck_calls": self.hourly_rechecks,
            "half_hour_diagnostic_calls": self.half_hour_diagnostics,
            "total_candle_api_calls": self.api_calls,
            "recovered_one_hour_candles": self.recovered,
            "lower_timeframe_bars_in_missing_hour": self.last_lower_tf_count,
            "source_limitations": (
                "UNKNOWN remains UNKNOWN unless exact Bithumb 1h candle was returned. "
                "30m is diagnosis only, not synthetic execution prices."
            ),
        }
