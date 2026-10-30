"""Totals and time series for the insights and charge pages, free of Dash so they can be unit tested."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

PERIODS = OrderedDict([("7d", ("7 days", 7, "day")), ("30d", ("30 days", 30, "day")),
                       ("365d", ("12 months", 365, "month")), ("all", ("All", None, "month"))])


def _aware(date):
    if date is None:
        return None
    if isinstance(date, str):
        date = datetime.fromisoformat(date)
    if date.tzinfo is None:
        return date.replace(tzinfo=timezone.utc)
    return date


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def period_start(period, now=None):
    days = PERIODS[period][1]
    if days is None:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def trip_rows(trips):
    """Trip objects (or dicts) as plain dicts with the fields the pages need."""
    rows = []
    for trip in trips or []:
        get = trip.get if isinstance(trip, dict) else lambda key, t=trip: getattr(t, key, None)
        rows.append({"start_at": _aware(get("start_at")), "distance": _num(get("distance")),
                     "consumption": _num(get("consumption")), "consumption_km": get("consumption_km"),
                     "duration": _num(get("duration")), "speed_average": get("speed_average")})
    return rows


def charge_rows(chargings, vin=None):
    rows = []
    for charge in chargings or []:
        # the rows feed the totals: an estimated charge has no energy, it must not count as a charge of 0 kWh
        if (vin and charge.get("VIN", vin) != vin) or charge.get("estimated"):
            continue
        rows.append({"start_at": _aware(charge.get("start_at")), "stop_at": _aware(charge.get("stop_at")),
                     "kw": _num(charge.get("kw")), "price": charge.get("price"),
                     "start_level": charge.get("start_level"), "end_level": charge.get("end_level")})
    return rows


def estimated_charge_rows(chargings, vin=None):
    """Estimated charges for display, keeping unknown values unknown. Never pass them to the totals."""
    return [{"start_at": _aware(charge["start_at"]), "stop_at": _aware(charge["stop_at"]),
             "start_level": charge["start_level"], "end_level": charge["end_level"], "estimated": True}
            for charge in chargings or [] if not vin or charge["VIN"] == vin]


def in_period(rows, period, now=None):
    start = period_start(period, now)
    return [row for row in rows if row["start_at"] and (start is None or row["start_at"] >= start)]


def trip_totals(trips):
    distance = sum(t["distance"] for t in trips)
    energy = sum(t["consumption"] for t in trips)
    return {"distance": distance, "count": len(trips), "energy": energy,
            "consumption": energy / distance * 100 if distance else None}


def charge_totals(charges):
    energy = sum(c["kw"] for c in charges)
    priced = [c for c in charges if isinstance(c["price"], (int, float))]
    cost = sum(c["price"] for c in priced)
    priced_energy = sum(c["kw"] for c in priced)
    durations = [(c["stop_at"] - c["start_at"]).total_seconds() / 3600 for c in charges
                 if c["stop_at"] and c["start_at"] and c["stop_at"] > c["start_at"]]
    hours = sum(durations)
    charged_with_duration = sum(c["kw"] for c in charges if c["stop_at"] and c["start_at"]
                                and c["stop_at"] > c["start_at"])
    return {"energy": energy, "count": len(charges), "cost": cost if priced else None,
            "price_per_kwh": cost / priced_energy if priced_energy else None,
            "avg_power": charged_with_duration / hours if hours else None}


def _bucket(date, unit):
    local = date.astimezone()
    if unit == "day":
        return local.date()
    return local.date().replace(day=1)


def local_series(rows, value_key, period, divisor_key=None, now=None):
    """What clientside.js needs to redo series() with the days of the browser, which aren't the ones of the server.
    With divisor_key, the buckets hold value per 100 of divisor."""
    rows = in_period(rows, period, now)
    start = period_start(period, now) or min(row["start_at"] for row in rows)
    return {"unit": PERIODS[period][2], "start": start.timestamp(), "per_100": divisor_key is not None,
            "end": (now or datetime.now(timezone.utc)).timestamp(),
            "rows": [[row["start_at"].timestamp(), _num(row[value_key]), _num(row[divisor_key]) if divisor_key else 0]
                     for row in rows]}


def series(rows, value_key, period, now=None):
    """Sum of value_key per day or month over the period, with empty buckets filled with 0."""
    unit = PERIODS[period][2]
    rows = in_period(rows, period, now)
    sums = OrderedDict()
    if rows:
        start = period_start(period, now) or min(row["start_at"] for row in rows)
        end = (now or datetime.now(timezone.utc))
        current = _bucket(start, unit)
        last = _bucket(end, unit)
        while current <= last:
            sums[current] = 0
            if unit == "day":
                current += timedelta(days=1)
            else:
                current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    for row in rows:
        key = _bucket(row["start_at"], unit)
        sums[key] = sums.get(key, 0) + _num(row[value_key])
    return list(sums.keys()), list(sums.values())
