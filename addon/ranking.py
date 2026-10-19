"""Turns the server's raw rows into standings.

The stats and the league follow the original Anki Leaderboard add-on
(ThoreBor/Anki_Leaderboard, MIT): reviews, minutes, streak, reviews in the past
31 days and retention, plus two-week league seasons ranked by XP.
"""

import datetime

DAY = datetime.timedelta(days=1)
SEASON_DAYS = 14
DEFAULT_SEASON_START = "2026-10-19"  # a Monday; admins can move it in group settings
LEAGUE_DAY_SECONDS = 5 * 60  # a day counts as studied with at least 5 minutes
MEDALS = ("gold", "silver", "bronze")
# The groups of Anki's Reviews graph, plus cards added.
BREAKDOWN = ("learn", "young", "mature", "relearn", "added")
SUMMED = ("reviews", "seconds", "again") + BREAKDOWN


def standings(board: dict, today: datetime.date, start=None, end=None) -> dict:
    """Stats per person for start..end (inclusive dates); None means all time."""
    players = board.get("players", [])
    daily = _daily_by_player(board)
    past31 = today - 30 * DAY

    people = []
    for p in players:
        days = daily.get(p["id"], {})
        if start is None:
            totals = {k: _num(p.get(k)) for k in SUMMED}
        else:
            totals = _sum(days, start, end or today)
        people.append(
            {
                "id": p["id"],
                "name": p["name"],
                "reviews": int(totals["reviews"]),
                "minutes": round(totals["seconds"] / 60, 1),
                "retention": _retention(totals),
                "secPerCard": _sec_per_card(totals),
                "streak": _streak(p, today),
                "past31": int(_sum(days, past31, today)["reviews"]),
                **{k: int(totals[k]) for k in BREAKDOWN},
                "updated": p.get("updated", ""),
            }
        )
    people.sort(key=lambda x: (-x["reviews"], x["name"].lower()))
    return {
        "me": (board.get("me") or {}).get("id"),
        "settings": board.get("settings") or {},
        "people": people,
    }


def league(board: dict, today: datetime.date) -> dict:
    """The current season's XP table and everyone's medals from past seasons."""
    settings = board.get("settings") or {}
    anchor = _date(settings.get("seasonStart")) or _date(DEFAULT_SEASON_START)
    daily = _daily_by_player(board)
    players = board.get("players", [])

    if today < anchor:
        return {"season": None, "startsOn": anchor.isoformat(), "rows": [], "medals": {}}

    number = (today - anchor).days // SEASON_DAYS + 1
    medals = {p["id"]: {m: 0 for m in MEDALS} for p in players}
    for past in range(1, number):
        start = anchor + (past - 1) * SEASON_DAYS * DAY
        rows = _season_rows(players, daily, start, start + (SEASON_DAYS - 1) * DAY, today)
        for medal, row in zip(MEDALS, [r for r in rows if r["xp"] > 0]):
            medals[row["id"]][medal] += 1

    start = anchor + (number - 1) * SEASON_DAYS * DAY
    end = start + (SEASON_DAYS - 1) * DAY
    return {
        "season": number,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daysLeft": (end - today).days + 1,
        "rows": _season_rows(players, daily, start, end, today),
        "medals": medals,
    }


def chart(board: dict, start: datetime.date, end: datetime.date, unit: str) -> dict:
    """Per-person series for start..end in buckets of a day, week or month.

    Colors follow join order (the players list order), never rank, so a
    person keeps their color whatever the range or sort.
    """
    buckets = _buckets(start, end, unit)
    daily = _daily_by_player(board)
    series = []
    for slot, p in enumerate(board.get("players", [])):
        days = daily.get(p["id"], {})
        values = {"reviews": [], "minutes": [], "secPerCard": [], "retention": [], "added": []}
        for b_start, b_end, _ in buckets:
            t = _sum(days, b_start, b_end)
            values["reviews"].append(int(t["reviews"]))
            values["minutes"].append(round(t["seconds"] / 60, 1))
            values["secPerCard"].append(_sec_per_card(t) if t["reviews"] else None)
            values["retention"].append(_retention(t) if t["reviews"] else None)
            values["added"].append(int(t["added"]))
        series.append({"id": p["id"], "name": p["name"], "slot": slot, "values": values})
    return {"unit": unit, "labels": [label for _, _, label in buckets], "series": series}


def _buckets(start, end, unit):
    out = []
    if unit == "day":
        d = start
        while d <= end:
            out.append((d, d, f"{d:%b} {d.day}"))
            d += DAY
    elif unit == "week":
        d = start - start.weekday() * DAY
        while d <= end:
            out.append((max(d, start), min(d + 6 * DAY, end), f"{d:%b} {d.day}"))
            d += 7 * DAY
    else:
        d = start.replace(day=1)
        while d <= end:
            nxt = (d.replace(day=28) + 4 * DAY).replace(day=1)
            out.append((max(d, start), min(nxt - DAY, end), f"{d:%b} '{d:%y}"))
            d = nxt
    return out


def oldest_needed(today: datetime.date, settings: dict, start=None) -> datetime.date:
    """The earliest date the board must include for these standings and the league."""
    anchor = _date((settings or {}).get("seasonStart")) or _date(DEFAULT_SEASON_START)
    dates = [today - 30 * DAY, min(anchor, today)]
    if start is not None:
        dates.append(start)
    return min(dates)


def xp(days_percent: float, minutes: float, reviews: int, retention: float) -> int:
    return int(days_percent * (6 * minutes + 2 * reviews * _retention_bonus(retention)))


def _season_rows(players, daily, start, end, today):
    last = min(end, today)
    days_over = (last - start).days + 1
    rows = []
    for p in players:
        days = daily.get(p["id"], {})
        totals = _sum(days, start, last)
        learned = sum(
            1
            for date, d in days.items()
            if start <= date <= last and _num(d.get("seconds")) >= LEAGUE_DAY_SECONDS
        )
        days_percent = round(100 * learned / days_over, 1) if days_over > 0 else 0.0
        minutes = round(totals["seconds"] / 60, 1)
        retention = _retention(totals)
        rows.append(
            {
                "id": p["id"],
                "name": p["name"],
                "xp": xp(days_percent, minutes, int(totals["reviews"]), retention),
                "reviews": int(totals["reviews"]),
                "minutes": minutes,
                "retention": retention,
                "daysPercent": days_percent,
            }
        )
    rows.sort(key=lambda r: (-r["xp"], r["name"].lower()))
    return rows


def _retention_bonus(retention: float) -> float:
    for floor, bonus in ((85, 1), (70, 0.85), (55, 0.70), (40, 0.55), (25, 0.40), (10, 0.25)):
        if retention >= floor:
            return bonus
    return 0


def _daily_by_player(board):
    out = {}
    for d in board.get("days", []):
        date = _date(d.get("date"))
        if date:
            out.setdefault(d.get("player"), {})[date] = d
    return out


def _sum(days, start, end):
    totals = {k: 0.0 for k in SUMMED}
    for date, d in days.items():
        if start <= date <= end:
            for k in totals:
                totals[k] += _num(d.get(k))
    return totals


def _retention(totals) -> float:
    reviews = totals["reviews"]
    return round(100 * (reviews - totals["again"]) / reviews, 1) if reviews else 0.0


def _sec_per_card(totals) -> float:
    reviews = totals["reviews"]
    return round(totals["seconds"] / reviews, 1) if reviews else 0.0


def _streak(p, today) -> int:
    # A stored streak is stale once someone misses a whole day.
    last = _date(str(p.get("lastStudyDate", ""))[:10])
    return int(_num(p.get("streak"))) if last and last >= today - DAY else 0


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
