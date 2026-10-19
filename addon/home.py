"""A small leaderboard pinned under the deck list on Anki's home screen.

It shows the range and ranking someone pinned from the leaderboard window and
reads the saved board, so drawing it costs nothing. At most every 10 minutes
it asks the server whether anything changed (a tiny "unchanged" answer when
nothing did) and redraws only when something did.
"""

import datetime
import html

from aqt import gui_hooks, mw

from . import ranking, stats, window

ADDON = __name__.split(".")[0]
CHECK_EVERY = datetime.timedelta(minutes=10)
TOP = 5
OPEN_CMD = "friends_lb:open"
METRIC_LABELS = {
    "reviews": "Reviews",
    "minutes": "Minutes",
    "secPerCard": "Sec/card",
    "streak": "Streak",
    "past31": "Past 31 days",
    "retention": "Retention",
}

_open_board = None
_failed_at = None


def setup(open_board) -> None:
    global _open_board
    _open_board = open_board
    gui_hooks.deck_browser_will_render_content.append(_on_render)
    gui_hooks.webview_did_receive_js_message.append(_on_message)


def mark_stale() -> None:
    """After an upload, check for new results the next time the home screen draws."""
    cache = window.cached_board()
    if cache:
        cache["fetched"] = datetime.datetime.min


def _on_message(handled, message, context):
    if message == OPEN_CMD:
        if _open_board:
            _open_board()
        return (True, None)
    return handled


def _on_render(deck_browser, content) -> None:
    cfg = mw.addonManager.getConfig(ADDON)
    if not cfg.get("home_pin") or not cfg.get("token") or mw.col is None:
        return
    try:
        content.stats += _widget(cfg)
    except Exception:
        # Never break the home screen over the leaderboard.
        return
    _maybe_check(cfg)


def _widget(cfg) -> str:
    today = stats.anki_today(mw.col)
    rng = list(cfg.get("home_range") or ["today", None, None]) + [None, None]
    key, a, b = rng[0], rng[1], rng[2]
    metric = cfg.get("home_metric") or "reviews"
    if metric not in METRIC_LABELS:
        metric = "reviews"
    start, end, caption = window.range_dates(key, a, b, today=today)
    cache = window.cached_board()
    if not cache or cache["since"] > (start or today).isoformat():
        body = "<p style='margin:8px 0;opacity:.75'>Loading the leaderboard…</p>"
    else:
        st = ranking.standings(cache["board"], today, start, end)
        body = _table(_sorted(st["people"], metric), st["me"], metric)
    return f"""
<div style="max-width:560px;margin:18px auto 0;text-align:left;font-size:13px">
  <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
    <b style="font-size:14px">Friends Leaderboard</b>
    <span style="opacity:.75;flex:1">{html.escape(caption)}</span>
    <a href="#" onclick="pycmd('{OPEN_CMD}');return false;">Open</a>
  </div>
  {body}
</div>"""


def _table(people, me, metric) -> str:
    shown = people[:TOP]
    mine = next((i for i, p in enumerate(people) if p["id"] == me), None)
    if mine is not None and mine >= TOP:
        shown = people[: TOP - 1] + [people[mine]]
    rows = []
    for p in shown:
        rank = people.index(p) + 1
        you = p["id"] == me
        name = html.escape(p["name"]) + (" (you)" if you else "")
        style = "font-weight:650;" if you else ""
        rows.append(
            f"<tr style='{style}'><td style='opacity:.75;padding:4px 8px 4px 0'>{rank}</td>"
            f"<td style='padding:4px 8px;width:100%'>{name}</td>"
            f"<td style='padding:4px 0 4px 8px;text-align:right;white-space:nowrap;"
            f"font-variant-numeric:tabular-nums'>{_value(p, metric)}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td style='opacity:.75;padding:4px 0'>Nobody has joined yet.</td></tr>")
    label = html.escape(METRIC_LABELS[metric])
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        f"<tr style='opacity:.75;font-size:12px'><th></th><th style='text-align:left;padding:0 8px'>Name</th>"
        f"<th style='text-align:right'>{label}</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _value(p, metric) -> str:
    v = p.get(metric, 0)
    if metric in ("secPerCard", "retention") and not p.get("reviews"):
        return "–"
    if metric == "secPerCard":
        return f"{v:.1f}s"
    if metric == "retention":
        return f"{v:.1f}%"
    return f"{int(round(v)):,}"


def _sorted(people, metric):
    if metric == "secPerCard":
        return sorted(people, key=lambda p: (0 if p["reviews"] else 1, p["secPerCard"], p["name"].lower()))
    return sorted(people, key=lambda p: (-p.get(metric, 0), p["name"].lower()))


def _maybe_check(cfg) -> None:
    now = datetime.datetime.now()
    cache = window.cached_board()
    fresh = cache and now - cache["fetched"] < CHECK_EVERY
    backing_off = _failed_at is not None and now - _failed_at < CHECK_EVERY
    if fresh or backing_off or window.is_fetching():
        return
    today = stats.anki_today(mw.col)
    rng = list(cfg.get("home_range") or ["today", None, None]) + [None, None]
    settings = (cache or {}).get("board", {}).get("settings") or {}
    need = window.needed_since(today, settings, tuple(rng[:3]))
    before = (cache or {}).get("lastWrite")
    had_board = bool(cache)

    def done(board, error):
        global _failed_at
        if error is not None:
            _failed_at = datetime.datetime.now()
            return
        _failed_at = None
        after = (window.cached_board() or {}).get("lastWrite")
        if (after != before or not had_board) and mw.state == "deckBrowser":
            mw.deckBrowser.refresh()

    window.fetch_board(need, done)
