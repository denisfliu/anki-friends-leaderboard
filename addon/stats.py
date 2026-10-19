"""Reads review stats from the local collection.

Days follow Anki's own day boundary (the "next day starts at" setting), so the
numbers match what each person sees in Anki's stats screen. Only real answers
count (ease > 0); manual reschedules and resets are skipped.
"""

import datetime

HISTORY_DAYS = 14
# Full history goes up in chunks the server accepts (it takes 400 days per upload).
CHUNK_DAYS = 400


def anki_today(col) -> datetime.date:
    return datetime.datetime.fromtimestamp(col.sched.day_cutoff - 86400).date()


def latest_review_id(col) -> int:
    return col.db.scalar("select coalesce(max(id), 0) from revlog") or 0


def collect(col, days=None) -> dict:
    """Per-day rows for the last `days` days (None = all history) plus totals."""
    cutoff = col.sched.day_cutoff
    today = anki_today(col)
    since_ms = 0 if days is None else (cutoff - days * 86400) * 1000
    # 0 = today, 1 = yesterday, ...
    day = "(? - 1 - {col} / 1000) / 86400"

    per_day = {}
    for d, reviews, ms, again, rev_cards, rev_correct in col.db.all(
        f"select {day.format(col='id')} as d, count(), sum(time), sum(ease = 1),"
        " sum(type = 1), sum(type = 1 and ease > 1)"
        " from revlog where ease > 0 and id >= ? group by d",
        cutoff,
        since_ms,
    ):
        per_day[d] = {
            "reviews": reviews,
            "seconds": round((ms or 0) / 1000),
            "again": again or 0,
            "newCards": 0,
            "reviewCards": rev_cards or 0,
            "reviewCorrect": rev_correct or 0,
        }

    # A card counts as new on the day of its first answer.
    for d, n in col.db.all(
        f"select {day.format(col='first')} as d, count()"
        " from (select min(id) as first from revlog where ease > 0 group by cid)"
        " where first >= ? group by d",
        cutoff,
        since_ms,
    ):
        if d in per_day:
            per_day[d]["newCards"] = n

    rows = []
    for d in sorted(per_day, reverse=True):
        if d >= 0 and (days is None or d < days):
            rows.append(
                {"date": (today - datetime.timedelta(days=d)).isoformat(), **per_day[d]}
            )

    reviews, ms, again, cards = col.db.first(
        "select count(), coalesce(sum(time), 0), coalesce(sum(ease = 1), 0), count(distinct cid)"
        " from revlog where ease > 0"
    )
    studied = {
        d
        for d in col.db.list(
            f"select distinct {day.format(col='id')} from revlog where ease > 0",
            cutoff,
        )
        if d >= 0
    }

    return {
        "days": rows,
        "totals": {
            "reviews": reviews,
            "seconds": round(ms / 1000),
            "again": again,
            "cards": cards,
            "daysStudied": len(studied),
            "streak": current_streak(studied),
            "longestStreak": longest_streak(studied),
            "lastStudyDate": (
                (today - datetime.timedelta(days=min(studied))).isoformat()
                if studied
                else ""
            ),
        },
    }


def current_streak(studied: set) -> int:
    # Not studying yet today doesn't break the streak until the day ends.
    start = 0 if 0 in studied else 1
    n = 0
    while start + n in studied:
        n += 1
    return n


def longest_streak(studied: set) -> int:
    best = run = 0
    prev = None
    for d in sorted(studied):
        run = run + 1 if prev is not None and d == prev + 1 else 1
        best = max(best, run)
        prev = d
    return best
