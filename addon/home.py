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
<div style="max-width:720px;margin:18px auto 0;text-align:left;font-size:13px">
  <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
    <b style="font-size:14px">Friends Leaderboard</b>
    <span style="opacity:.75;flex:1">{html.escape(caption)}</span>
    <a href="#" onclick="pycmd('{OPEN_CMD}');return false;">Open</a>
  </div>
  {body}
</div>"""


# The same columns as the window's Leaderboard tab, in the same order.
COLUMNS = ["reviews", "minutes", "secPerCard", "streak", "past31", "retention"]
BORDER = "var(--border, rgba(128,128,128,.3))"


def _table(people, me, metric) -> str:
    cell = "padding:6px 8px;border-top:1px solid " + BORDER + ";"
    num = cell + "text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;"
    head = (
        "<tr style='opacity:.75;font-size:12px'>"
        "<th style='text-align:left;padding:0 8px 6px 0'>#</th>"
        "<th style='text-align:left;padding:0 8px 6px'>Name</th>"
        + "".join(
            f"<th style='text-align:right;padding:0 8px 6px;white-space:nowrap;"
            f"{'opacity:1;' if c == metric else ''}'>{html.escape(METRIC_LABELS[c])}</th>"
            for c in COLUMNS
        )
        + "</tr>"
    )
    rows = []
    for rank, p in enumerate(people, 1):
        you = p["id"] == me
        bg = "background:rgba(42,120,214,.13);" if you else ""
        name = html.escape(p["name"]) + (" (you)" if you else "")
        rows.append(
            f"<tr style='{bg}'>"
            f"<td style='{cell}padding-left:0;opacity:.75'>{rank}</td>"
            f"<td style='{cell}width:100%;{'font-weight:650;' if you else ''}'>{name}</td>"
            + "".join(
                f"<td style='{num}{'font-weight:650;' if c == metric else ''}'>{_value(p, c)}</td>"
                for c in COLUMNS
            )
            + "</tr>"
        )
    if not rows:
        rows.append(f"<tr><td colspan='8' style='{cell}opacity:.75'>Nobody has joined yet.</td></tr>")
    return "<table style='width:100%;border-collapse:collapse'>" + head + "".join(rows) + "</table>"


def _value(p, metric) -> str:
    v = p.get(metric, 0)
    if metric in ("secPerCard", "retention") and not p.get("reviews"):
        return "–"
    if metric == "secPerCard":
        return f"{v:.1f}"
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
