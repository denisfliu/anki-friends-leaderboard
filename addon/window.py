"""The leaderboard window: Leaderboard and League tabs in an Anki web view.

Python fetches the board and does the math (ranking.py); the page only lays
out what it's given, switches tabs, sorts columns and draws the chart. It asks
Python for a new date range through pycmd, and Python downloads older days
only when a range needs them.
"""

import datetime
import json
import os

from aqt import mw
from aqt.qt import QDialog, QVBoxLayout
from aqt.webview import AnkiWebView

from . import client, ranking, stats

DAY = datetime.timedelta(days=1)
ADDON = __name__.split(".")[0]

# Last board download, shared by the window and the home screen and kept on
# disk, so both show the last standings at once while checking for new ones.
# The server answers "unchanged" when nothing new was uploaded since.
_cache = None
_fetching = False
_dialog = None
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_files", "board.json")


def open_leaderboard(cfg: dict) -> None:
    global _dialog
    if _dialog is not None:
        _dialog.show()
        _dialog.raise_()
        _dialog.activateWindow()
        _dialog.load(check=True)
        return
    _dialog = LeaderboardDialog()
    _dialog.show()


def cached_board():
    _load_cache()
    return _cache


def forget_board() -> None:
    global _cache
    _cache = None
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass


def fetch_board(need: datetime.date, on_done) -> None:
    """Download the board from `need` on and call on_done(board, error) on the
    main thread. Asks for "unchanged" when the cache already covers `need`."""
    global _fetching
    cfg = mw.addonManager.getConfig(ADDON)
    since = need.isoformat()
    payload = {"action": "board", "token": cfg.get("token"), "since": since}
    _load_cache()
    if _cache and _cache["since"] <= since:
        payload["since"] = _cache["since"]
        payload["ifChangedSince"] = _cache["lastWrite"]
    url = cfg.get("server_url")
    _fetching = True

    def done(fut):
        global _cache, _fetching
        _fetching = False
        try:
            board = fut.result()
        except Exception as e:
            on_done(None, e)
            return
        if board.get("unchanged") and _cache:
            board = _cache["board"]
        else:
            _cache = {"since": payload["since"], "lastWrite": board.get("lastWrite", ""), "board": board}
        _cache["fetched"] = datetime.datetime.now()
        _save_cache()
        on_done(board, None)

    client.in_background(lambda: client.call(url, payload), done)


def is_fetching() -> bool:
    return _fetching


def _load_cache() -> None:
    global _cache
    if _cache is not None:
        return
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        saved["fetched"] = datetime.datetime.fromisoformat(saved["fetched"])
        _cache = saved
    except Exception:
        _cache = None


def _save_cache() -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data = dict(_cache, fetched=_cache["fetched"].isoformat())
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def needed_since(today, settings, rng) -> datetime.date:
    start, end, _ = range_dates(*rng, today=today)
    c_start, _, _ = chart_window(rng[0], start, end, today)
    return min(ranking.oldest_needed(today, settings, start), c_start)


class LeaderboardDialog(QDialog):
    def __init__(self):
        super().__init__(mw)
        self.board = None
        saved = mw.addonManager.getConfig(ADDON).get("last_range") or ["today", None, None]
        self.range = tuple((list(saved) + [None, None, None])[:3])
        self.setWindowTitle("Friends Leaderboard")
        self.web = AnkiWebView(parent=self, title="friends_leaderboard")
        self.web.set_bridge_command(self.on_command, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.resize(760, 760)
        self.web.stdHtml(PAGE, context=self)
        self.load(check=True)

    def done(self, result: int) -> None:
        global _dialog
        _dialog = None
        if hasattr(self.web, "cleanup"):
            self.web.cleanup()
        super().done(result)

    # -- page -> python

    def on_command(self, cmd: str):
        if cmd == "refresh":
            self.load(check=True)
        elif cmd.startswith("range:"):
            parts = cmd.split(":")
            self.range = (parts[1], parts[2] if len(parts) > 2 else None, parts[3] if len(parts) > 3 else None)
            cfg = mw.addonManager.getConfig(ADDON)
            cfg["last_range"] = list(self.range)
            if cfg.get("home_pin"):
                # The home screen shows whatever this window shows.
                cfg["home_range"] = list(self.range)
            mw.addonManager.writeConfig(ADDON, cfg)
            if cfg.get("home_pin") and mw.state == "deckBrowser":
                mw.deckBrowser.refresh()
            self.load()
        elif cmd.startswith("prefs:"):
            try:
                prefs = json.loads(cmd[len("prefs:"):])
            except ValueError:
                return
            if isinstance(prefs, dict):
                cfg = mw.addonManager.getConfig(ADDON)
                cfg["view_prefs"] = {k: prefs[k] for k in ("tab", "sort", "chart", "show", "off") if k in prefs}
                mw.addonManager.writeConfig(ADDON, cfg)
        elif cmd.startswith("pin:"):
            _, state, metric = (cmd.split(":") + ["", ""])[:3]
            cfg = mw.addonManager.getConfig(ADDON)
            cfg["home_pin"] = state == "on"
            cfg["home_range"] = list(self.range)
            cfg["home_metric"] = metric or "reviews"
            mw.addonManager.writeConfig(ADDON, cfg)
            if mw.state == "deckBrowser":
                mw.deckBrowser.refresh()
            self.load()

    # -- data

    def load(self, check: bool = False) -> None:
        """Show the current range. `check` asks the server whether anything
        changed; otherwise cached data is used when it covers the range."""
        if mw.col is None:
            return
        today = stats.anki_today(mw.col)
        cache = cached_board()
        settings = (cache or {}).get("board", {}).get("settings") or {}
        need = needed_since(today, settings, self.range)
        if cache and cache["since"] <= need.isoformat():
            self.board = cache["board"]
            self.render(today)
            if not check:
                return
        self.web.eval("lb.loading(true)")

        def on_done(board, error):
            if _dialog is not self:
                return
            if error is not None:
                message = str(error) if isinstance(error, client.ServerError) else "Anki couldn't reach the server."
                self.web.eval(f"lb.error({json.dumps(message)})")
                return
            self.board = board
            # The season start lives in group settings; fetch again if it
            # turns out to need older days than this download covered.
            if needed_since(today, board.get("settings"), self.range).isoformat() < _cache["since"]:
                self.load(check=True)
                return
            self.render(today)

        fetch_board(need, on_done)

    def render(self, today: datetime.date) -> None:
        board = self.board or {}
        start, end, caption = range_dates(*self.range, today=today)
        st = ranking.standings(board, today, start, end)
        lg = ranking.league(board, today)
        medals = lg.get("medals", {})
        for row in lg.get("rows", []):
            row["medals"] = medals.get(row["id"], {})
        c_start, c_end, unit = chart_window(self.range[0], start, end, today)
        cfg = mw.addonManager.getConfig(ADDON)
        pinned = bool(cfg.get("home_pin"))
        fetched = (_cache or {}).get("fetched")
        view = {
            "me": st["me"],
            "announcement": str(st["settings"].get("announcement") or ""),
            "updated": _updated_text(fetched) if fetched else "",
            "pinned": pinned,
            "prefs": cfg.get("view_prefs") or {},
            "pinnedMetric": cfg.get("home_metric") or "reviews",
            "range": {
                "key": self.range[0],
                "caption": caption,
                "from": (start or today).isoformat(),
                "to": (end or today).isoformat(),
                "today": today.isoformat(),
            },
            "people": st["people"],
            "chart": dict(ranking.chart(board, c_start, c_end, unit), caption=chart_caption(c_start, c_end, unit)),
            "league": {
                "season": lg.get("season"),
                "startsOn": _pretty(lg.get("startsOn")),
                "start": _pretty(lg.get("start")),
                "end": _pretty(lg.get("end")),
                "daysLeft": lg.get("daysLeft"),
                "rows": lg.get("rows", []),
            },
        }
        self.web.eval(f"lb.render({json.dumps(view)})")


def range_dates(key, a=None, b=None, *, today):
    """(start, end, caption) for a range key; start None means all time."""
    if key == "week":
        start = today - today.weekday() * DAY
        return start, today, _span(start, start + 6 * DAY)
    if key == "month":
        return today.replace(day=1), today, today.strftime("%B %Y")
    if key == "year":
        return today.replace(month=1, day=1), today, today.strftime("%Y")
    if key == "all":
        return None, None, ""
    if key == "custom":
        start, end = _parse(a) or today, _parse(b) or today
        if start > end:
            start, end = end, start
        end = min(end, today)
        start = min(start, end)
        return start, end, _span(start, end)
    return today, today, _short(today)


def _span(start, end) -> str:
    """"Oct 19 to 25", "Oct 26 to Nov 1", "Dec 29, 2025 to Jan 4, 2026"."""
    if start.year != end.year:
        return f"{start:%b} {start.day}, {start.year} to {end:%b} {end.day}, {end.year}"
    if start.month != end.month:
        return f"{start:%b} {start.day} to {end:%b} {end.day}"
    return f"{start:%b} {start.day} to {end.day}"


def chart_window(key, start, end, today):
    """(start, end, unit) for the chart under a range. Short ranges widen to
    at least a week so there's a line to read; long ones use weeks or months."""
    if start is None:  # all time: the last 12 months
        first = today.replace(day=1)
        for _ in range(11):
            first = (first - DAY).replace(day=1)
        return first, today, "month"
    end = end or today
    start = min(start, end - 13 * DAY if key == "today" else end - 6 * DAY)
    span = (end - start).days + 1
    unit = "day" if span <= 62 else "week" if span <= 400 else "month"
    return start, end, unit


def chart_caption(start, end, unit) -> str:
    return f"{_span(start, end)}, by {unit}"


def _updated_text(fetched: datetime.datetime) -> str:
    """"9:41 AM" today, "yesterday 9:41 PM", or "Oct 18" for older data."""
    clock = fetched.strftime("%I:%M %p").lstrip("0")
    days = (datetime.date.today() - fetched.date()).days
    if days <= 0:
        return clock
    if days == 1:
        return "yesterday " + clock
    return f"{fetched:%b} {fetched.day}"


def _parse(v):
    try:
        return datetime.date.fromisoformat(v or "")
    except ValueError:
        return None


def _short(d: datetime.date) -> str:
    return f"{d:%a, %b} {d.day}"


def _pretty(iso):
    d = _parse(iso)
    return _short(d) if d else ""


PAGE = r"""
<style>
  body {
    --bg: #ffffff; --surface: #f5f5f7; --track: #ececf0; --text: #1d1d1f;
    --muted: #5f6368; --line: #e2e2e6; --grid: #ececef; --accent: #1f5fc9; --you: #e8f0fe;
    --gold-bg: #fbefc8; --gold-fg: #6b4e00; --silver-bg: #e9ecf0; --silver-fg: #454b55;
    --bronze-bg: #f6e1d0; --bronze-fg: #7a4318;
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
    --s5: #e87ba4; --s6: #008300; --s7: #4a3aa7; --s8: #e34948;
    --other: #d2d2d7; --chip: #eaf1fd;
    margin: 0; background: var(--bg); color: var(--text);
    font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
  }
  body.night-mode, body.nightMode, body.night_mode, html.night-mode body {
    --bg: #232326; --surface: #2d2d31; --track: #1b1b1d; --text: #ececef;
    --muted: #a8a8b0; --line: #3a3a40; --grid: #313136; --accent: #7aa7ff; --you: #26344d;
    --gold-bg: #4a3b14; --gold-fg: #f3d27a; --silver-bg: #3b3e44; --silver-fg: #d5d9e0;
    --bronze-bg: #45301f; --bronze-fg: #e9b48a;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
    --other: #4a4a52; --chip: #22324d;
  }
  button, select, input { font: inherit; color: var(--text); }
  .wrap { position: relative; padding: 0 22px 18px; display: flex; flex-direction: column; gap: 14px; min-height: 100vh; box-sizing: border-box; }

  /* In-page date picker and menu. Native pickers and dropdowns open macOS
     popups that crash Anki's web view, so nothing here opens one. */
  .datefield { width: 116px; height: 30px; padding: 0 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); font-variant-numeric: tabular-nums; }
  .datefield:focus, .menubtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .datefield.bad { border-color: #b3261e; }
  .pop { position: absolute; z-index: 10; background: var(--bg); border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.18); padding: 10px; }
  .cal-head { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
  .cal-head b { flex: 1; text-align: center; }
  .cal-nav { width: 30px; height: 30px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .cal-nav:disabled { opacity: .35; cursor: default; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 34px); gap: 2px; }
  .cal-grid .dow { text-align: center; color: var(--muted); font-size: 11px; padding: 4px 0; }
  .cal-grid button { height: 32px; border: 0; border-radius: 6px; background: none; cursor: pointer; font-variant-numeric: tabular-nums; }
  .cal-grid button:hover:not(:disabled) { background: var(--track); }
  .cal-grid button.today { box-shadow: inset 0 0 0 1px var(--accent); }
  .cal-grid button.picked { background: var(--accent); color: var(--bg); font-weight: 650; }
  .cal-grid button:disabled { color: var(--muted); opacity: .45; cursor: default; }
  .menubtn { height: 30px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; min-width: 120px; text-align: left; }
  .menu { display: flex; flex-direction: column; padding: 4px; min-width: 210px; }
  .menu button { text-align: left; height: 32px; padding: 0 10px; border: 0; border-radius: 6px; background: none; cursor: pointer; }
  .menu button:hover, .menu button:focus { background: var(--track); outline: none; }
  .menu button[aria-checked="true"] { font-weight: 650; }
  .menu button[aria-checked="true"]::after { content: " ✓"; color: var(--accent); }
  .tabs { display: flex; align-items: flex-end; gap: 4px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .tab { height: 44px; padding: 0 14px; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; color: var(--muted); cursor: pointer; }
  .tab[aria-selected="true"] { border-bottom-color: var(--accent); color: var(--text); font-weight: 650; }
  .spacer { flex: 1; }
  .status { align-self: center; color: var(--muted); font-size: 11px; margin-right: 2px; white-space: nowrap; }
  .icon { align-self: center; width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 6px; background: none; color: var(--muted); cursor: pointer; }
  .icon:hover { background: var(--track); color: var(--text); }
  .icon:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .icon[aria-pressed="true"] { color: var(--accent); }
  .icon[aria-pressed="true"] .pin-head { fill: currentColor; }
  .icon.spinning svg { animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .btn { height: 28px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .btn[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .tabs .btn { align-self: center; margin-left: 6px; }
  .seg { display: flex; padding: 3px; gap: 2px; border-radius: 8px; background: var(--track); }
  .seg button { flex: none; height: 28px; padding: 0 12px; border: 0; border-radius: 6px; background: none; color: var(--muted); cursor: pointer; }
  .controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .range-caption { flex: 1; min-width: 0; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .controls #rankby { flex-wrap: nowrap; }
  .controls .menubtn { min-width: 0; }
  .rank-badge { display: inline-flex; width: 22px; height: 22px; margin: -4px 0; align-items: center; justify-content: center; border-radius: 11px; font-size: 12px; font-weight: 700; }
  .rank-num { display: inline-flex; width: 22px; justify-content: center; font-variant-numeric: tabular-nums; }
  .rank-badge.sm { width: 16px; height: 16px; border-radius: 8px; font-size: 10px; margin-left: 6px; vertical-align: 1px; }
  .lb tr.idle td, .lb tr.idle td.main, .bar-row.idle .who, .bar-row.idle .total { color: var(--muted); font-weight: 400; }
  .lb tr.pickable { cursor: pointer; }
  .lb tr.pickable:hover td { background: var(--track); }
  .lb tr.comparing td:first-child { box-shadow: inset 3px 0 0 var(--s2); }
  .lb th button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
  .chart-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .seg.small button { height: 26px; padding: 0 11px; }
  .muted { color: var(--muted); }
  .chip { height: 28px; padding: 0 12px; border-radius: 14px; border: 1px solid var(--accent); background: var(--chip); color: var(--text); font-weight: 600; cursor: pointer; }
  .legend-item { all: unset; display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
  button.legend-item { cursor: pointer; }
  button.legend-item:hover { color: var(--text); }
  button.legend-item:focus-visible { outline: 2px solid var(--accent); border-radius: 3px; }
  .trends { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 28px; }
  .trend-row { all: unset; display: grid; grid-template-columns: minmax(0, 1fr) 150px 64px; gap: 10px; align-items: center; height: 30px; padding: 0 6px; border-radius: 6px; }
  button.trend-row { cursor: pointer; }
  button.trend-row:hover { background: var(--track); }
  button.trend-row:focus-visible { outline: 2px solid var(--accent); }
  .trend-row.you .who { font-weight: 650; }
  .trend-row.comparing { box-shadow: inset 3px 0 0 var(--s2); }
  .trend-row .who { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .trend-row .total { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
  .menu .checkbox { width: 14px; height: 14px; border-radius: 3px; border: 1.5px solid var(--muted); box-sizing: border-box; flex: none; margin-right: 8px; }
  .menu button[role=menuitemcheckbox] { display: flex; align-items: center; }
  .menu button[role=menuitemcheckbox] span { flex: 1; }
  .menu button[role=menuitemcheckbox] .key { width: 14px; height: 2px; }
  .menu button[role=menuitemcheckbox][aria-checked="true"] .checkbox { background: var(--accent); border-color: var(--accent); }
  .menu button[role=menuitemcheckbox][aria-checked="true"]::after { content: none; }
  .menu button.separated { border-top: 1px solid var(--line); border-radius: 0 0 6px 6px; }
  .seg-bar.unknown { background: var(--track); }
  .seg button[aria-pressed="true"] { background: var(--bg); color: var(--text); font-weight: 600; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .caption { flex: 1; color: var(--muted); }
  label { color: var(--muted); }
  select, input[type=date] { height: 30px; padding: 0 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); }
  .custom { display: none; }
  .custom.open { display: flex; }
  table.lb { width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-variant-numeric: tabular-nums; }
  .lb th { background: var(--surface); color: var(--muted); font-size: 12px; font-weight: 600; padding: 9px 10px; text-align: right; white-space: nowrap; }
  .lb th button { all: unset; cursor: pointer; }
  .lb th.sorted { color: var(--text); }
  .lb td { padding: 10px; border-top: 1px solid var(--line); text-align: right; white-space: nowrap; }
  .lb th.l, .lb td.l { text-align: left; }
  .lb td.rank { color: var(--muted); width: 22px; }
  .lb td.name { width: 99%; overflow: hidden; text-overflow: ellipsis; max-width: 0; }
  .lb td.main { font-weight: 600; }
  .lb tr.you td { background: var(--you); }
  .lb tr.you td.name { font-weight: 650; }
  .medal { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 12px; font-weight: 600; margin-left: 4px; }
  .gold { background: var(--gold-bg); color: var(--gold-fg); }
  .silver { background: var(--silver-bg); color: var(--silver-fg); }
  .bronze { background: var(--bronze-bg); color: var(--bronze-fg); }
  .season { display: flex; align-items: baseline; gap: 10px; }
  .season h2 { margin: 0; font-size: 17px; font-weight: 650; }
  .note { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.5; }
  .announce { margin: 0; padding: 10px 12px; border-radius: 8px; background: var(--chip); color: var(--text); }
  .empty { color: var(--muted); padding: 24px 0; text-align: center; }
  .error { color: #b3261e; }
  body.night-mode .error, body.nightMode .error, body.night_mode .error, html.night-mode body .error { color: #f2b8b5; }
  [hidden] { display: none !important; }

  .groups { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--muted); font-size: 12px; }
  .groups span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .g-learn { background: var(--s1); } .g-young { background: var(--s2); }
  .g-mature { background: var(--s3); } .g-relearn { background: var(--s4); }
  .bars { display: flex; flex-direction: column; gap: 10px; position: relative; }
  .bar-row { display: grid; grid-template-columns: 140px minmax(0, 1fr) 64px; align-items: center; gap: 10px; }
  .bar-row .who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-row.you .who { font-weight: 650; }
  .bar-row .total { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
  .track { display: flex; gap: 2px; height: 16px; }
  .seg-bar { height: 16px; min-width: 2px; cursor: default; }
  .track { border-radius: 0 4px 4px 0; overflow: hidden; }
  .seg-bar:hover, .seg-bar:focus { filter: brightness(1.15); outline: none; }

  .chart { position: relative; border: 1px solid var(--line); border-radius: 8px; padding: 14px 14px 10px; display: flex; flex-direction: column; gap: 10px; }
  .chart-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .chart-head h3 { margin: 0; font-size: 14px; font-weight: 650; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--muted); font-size: 12px; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .key { width: 14px; height: 2px; border-radius: 1px; display: inline-block; }
  .plot { position: relative; }
  .plot svg { display: block; width: 100%; overflow: visible; }
  .plot svg:focus { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .axis { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .dlabel { fill: var(--muted); font-size: 11px; }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; box-shadow: 0 4px 14px rgba(0,0,0,.12); font-size: 12px; min-width: 130px; }
  .tip .when { color: var(--muted); margin-bottom: 4px; }
  .tip div.r { display: flex; align-items: center; gap: 8px; line-height: 1.6; }
  .tip b { font-variant-numeric: tabular-nums; }
  .tip .who { color: var(--muted); }
  .chart table { border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
  .chart-table { overflow-x: auto; }
  .chart-table th, .chart-table td { padding: 4px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--line); }
  .chart-table th:first-child, .chart-table td:first-child { text-align: left; }
  .chart-table th { color: var(--muted); font-weight: 600; }
  .linkbtn { all: unset; color: var(--accent); cursor: pointer; font-size: 12px; }
  .linkbtn:focus-visible { outline: 2px solid var(--accent); border-radius: 3px; }
</style>
<div class="wrap">
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="tab-board" aria-selected="true" onclick="lb.tab('board')">Leaderboard</button>
    <button class="tab" role="tab" id="tab-breakdown" aria-selected="false" onclick="lb.tab('breakdown')">Breakdown</button>
    <button class="tab" role="tab" id="tab-league" aria-selected="false" onclick="lb.tab('league')">League</button>
    <span class="spacer"></span>
    <span class="status" id="status"></span>
    <button class="icon" id="refresh" aria-label="Refresh" title="Refresh" onclick="pycmd('refresh')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button>
    <button class="icon" id="pin" aria-pressed="false" aria-label="Pin to home screen" title="Pin to home screen" onclick="lb.togglePin()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path class="pin-head" d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg></button>
  </div>
  <p class="announce" id="announce" hidden></p>

  <section id="range-block" style="display:flex;flex-direction:column;gap:14px">
      <div class="controls">
        <div class="seg" role="group" aria-label="Date range" id="ranges">
          <button data-key="today">Today</button>
          <button data-key="week">Week</button>
          <button data-key="month">Month</button>
          <button data-key="year">Year</button>
          <button data-key="all">All time</button>
          <button data-key="custom">Custom…</button>
        </div>
        <span class="range-caption" id="caption"></span>
        <span class="row" id="rankby">
        <span id="metric-label">Rank by</span>
        <button class="menubtn" id="metric" aria-haspopup="menu" aria-expanded="false" aria-labelledby="metric-label metric" onclick="lb.openMenu()"></button>
        </span>
      </div>
      <div class="row custom" id="custom">
        <label for="from">From</label>
        <input type="text" class="datefield" id="from" placeholder="YYYY-MM-DD" autocomplete="off" spellcheck="false">
        <label for="to">To</label>
        <input type="text" class="datefield" id="to" placeholder="YYYY-MM-DD" autocomplete="off" spellcheck="false">
        <button class="btn" onclick="lb.applyCustom()">Show</button>
      </div>
  </section>

  <section id="panel-board">
    <div style="display:flex;flex-direction:column;gap:14px">
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="reviews" title="Answers given in this range"><button>Reviews</button></th>
          <th data-sort="minutes" title="Time spent answering"><button>Minutes</button></th>
          <th data-sort="secPerCard" title="Average seconds per answer"><button>Sec/card</button></th>
          <th data-sort="streak" title="Days in a row with at least one review"><button>Streak</button></th>
          <th data-sort="past31" title="Reviews in the last 31 days, whatever the range"><button>Past 31 days</button></th>
          <th data-sort="retention" title="Share of answers that weren't Again"><button>Retention</button></th>
          <th data-sort="added" title="Cards created in this range"><button>Added</button></th>
        </tr></thead>
        <tbody id="board-rows"></tbody>
      </table>

      <section class="chart" aria-labelledby="chart-title">
        <div class="chart-bar">
          <div class="seg small" role="group" aria-label="Chart type" id="chart-types">
            <button data-chart="day">Per day</button>
            <button data-chart="race">Race</button>
            <button data-chart="avg">You vs average</button>
            <button data-chart="trend">Trends</button>
          </div>
          <span class="spacer"></span>
          <button class="menubtn" id="show-btn" aria-haspopup="menu" aria-expanded="false" onclick="lb.openShow()"></button>
          <button class="menubtn" id="hl-btn" aria-haspopup="menu" aria-expanded="false" onclick="lb.openHighlight()"></button>
          <button class="chip" id="compare-chip" hidden onclick="lb.endCompare()"></button>
        </div>
        <div class="chart-head">
          <h3 id="chart-title"></h3>
          <span class="caption" id="chart-caption"></span>
          <button class="linkbtn" id="chart-toggle" onclick="lb.toggleChartTable()">Show as table</button>
        </div>
        <div class="legend" id="legend"></div>
        <div class="plot" id="plot">
          <svg id="svg" role="img" tabindex="0"></svg>
          <div class="tip" id="tip" hidden></div>
        </div>
        <div class="trends" id="trends" hidden></div>
        <div class="chart-table" id="chart-table" hidden></div>
      </section>
    </div>
  </section>

  <section id="panel-breakdown" hidden>
    <div style="display:flex;flex-direction:column;gap:14px">
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="reviews" title="Answers given in this range"><button>Reviews</button></th>
          <th data-sort="learn" title="A new card's first steps"><button>Learning</button></th>
          <th data-sort="young" title="Reviews of cards with intervals under 21 days"><button>Young</button></th>
          <th data-sort="mature" title="Reviews of cards with intervals of 21 days or more"><button>Mature</button></th>
          <th data-sort="relearn" title="Forgotten cards going back through their steps"><button>Relearning</button></th>
          <th data-sort="added" title="Cards created in this range"><button>Added</button></th>
        </tr></thead>
        <tbody id="breakdown-rows"></tbody>
      </table>
      <section class="chart" aria-labelledby="mix-title">
        <div class="chart-head"><h3 id="mix-title">Answers by kind</h3><span class="caption" id="mix-caption"></span></div>
        <div class="groups" aria-hidden="true">
          <span><i class="swatch g-learn"></i>Learning</span>
          <span><i class="swatch g-young"></i>Young</span>
          <span><i class="swatch g-mature"></i>Mature</span>
          <span><i class="swatch g-relearn"></i>Relearning</span>
        </div>
        <div class="bars" id="bars"></div>
        <div class="tip" id="mix-tip" hidden></div>
      </section>
    </div>
  </section>

  <section id="panel-league" hidden>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="season"><h2 id="season-title"></h2><span class="caption" id="season-dates"></span><b id="season-left"></b></div>
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="xp" title="Days studied % × (6 × minutes + 2 × reviews × retention bonus). A day counts with at least 5 minutes."><button>XP</button></th>
          <th data-sort="reviews" title="Answers given this season"><button>Reviews</button></th>
          <th data-sort="minutes" title="Time spent answering this season"><button>Minutes</button></th>
          <th data-sort="retention" title="Share of answers that weren't Again"><button>Retention</button></th>
          <th data-sort="daysPercent" title="Days so far this season with at least 5 minutes of study"><button>Days</button></th>
          <th id="medals-th">Medals</th>
        </tr></thead>
        <tbody id="league-rows"></tbody>
      </table>
    </div>
  </section>

  <div class="pop" id="cal" role="dialog" aria-label="Choose a date" hidden></div>
  <div class="pop menu" id="menu" role="menu" hidden></div>
</div>
<script>
const lb = (() => {
  let view = null;
  let hover = null;
  let currentTab = "board";
  let menuAnchor = null;
  // Each table keeps its own sort; a second click on a heading reverses it.
  const sort = { board: { key: "reviews", dir: "desc" }, breakdown: { key: "reviews", dir: "desc" }, league: { key: "xp", dir: "desc" } };
  // Chart state; type, measure and who is switched off are remembered.
  const chart = { type: "day", show: null, off: new Set(), compare: null };
  const GROUPS = [
    { key: "learn", label: "Learning", cls: "g-learn" },
    { key: "young", label: "Young", cls: "g-young" },
    { key: "mature", label: "Mature", cls: "g-mature" },
    { key: "relearn", label: "Relearning", cls: "g-relearn" },
  ];
  const DETAIL_KEYS = ["learn", "young", "mature", "relearn", "added"];
  const NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n || 0).toLocaleString();
  const METRICS = {
    reviews: { label: "Reviews", fmt: (v) => fmt(v) },
    minutes: { label: "Minutes", fmt: (v) => fmt(Math.round(v)) },
    secPerCard: { label: "Seconds per card", fmt: (v) => v.toFixed(1) + "s" },
    retention: { label: "Retention", fmt: (v) => v.toFixed(1) + "%" },
    added: { label: "Cards added", fmt: (v) => fmt(v) },
  };
  const CHARTABLE = ["reviews", "minutes", "secPerCard", "retention", "added"];
  const RANKS = [
    { key: "reviews", label: "Reviews", short: "Reviews" },
    { key: "minutes", label: "Minutes", short: "Minutes" },
    { key: "secPerCard", label: "Seconds per card (fastest)", short: "Sec/card" },
    { key: "streak", label: "Streak", short: "Streak" },
    { key: "past31", label: "Past 31 days", short: "Past 31 days" },
    { key: "retention", label: "Retention", short: "Retention" },
    { key: "added", label: "Cards added", short: "Added" },
  ];
  const MEDAL = ["gold", "silver", "bronze"];

  // ---- values, sorting, medals, gaps --------------------------------------

  const defaultDir = (key) => (key === "secPerCard" ? "asc" : "desc");

  // null means "unknown" (no reviews to average, or an add-on that doesn't
  // send this number), which always sorts last and shows as a dash.
  function value(row, key) {
    if ((key === "secPerCard" || key === "retention") && !row.reviews) return null;
    if (DETAIL_KEYS.indexOf(key) >= 0 && row.detailed === false) return null;
    const v = row[key];
    return v === undefined ? null : v;
  }

  function sorted(rows, key, dir) {
    const sign = dir === "asc" ? 1 : -1;
    return rows.slice().sort((a, b) => {
      const va = value(a, key), vb = value(b, key);
      if (va === null && vb === null) return a.name.localeCompare(b.name);
      if (va === null) return 1;
      if (vb === null) return -1;
      return sign * (va - vb) || a.name.localeCompare(b.name);
    });
  }

  function medalsBy(rows, key) {
    const out = {};
    sorted(rows.filter((r) => (value(r, key) || 0) > 0), key, "desc").slice(0, 3).forEach((r, k) => { out[r.id] = k; });
    return out;
  }

  function badge(k, small, what) {
    const b = document.createElement("span");
    b.className = "rank-badge " + MEDAL[k] + (small ? " sm" : "");
    b.textContent = k + 1;
    b.title = ["Most ", "Second most ", "Third most "][k] + what;
    return b;
  }

  function td(text, cls) {
    const el = document.createElement("td");
    if (cls) el.className = cls;
    el.textContent = text;
    return el;
  }

  function nameCell(row, medals, sortState, medalKey, what) {
    const cell = td(row.id === view.me ? row.name + " (you)" : row.name, "name l");
    const k = medals[row.id];
    const big = sortState.key === medalKey && sortState.dir === "desc";
    if (k !== undefined && !big) cell.append(badge(k, true, what));
    return cell;
  }

  function rankCell(row, i, medals, sortState, medalKey, what) {
    const cell = td("", "rank l");
    const k = medals[row.id];
    if (k !== undefined && sortState.key === medalKey && sortState.dir === "desc") {
      cell.append(badge(k, false, what));
    } else {
      const n = document.createElement("span");
      n.className = "rank-num";
      n.textContent = i + 1;
      cell.append(n);
    }
    return cell;
  }

  function markHeaders(panel) {
    const st = sort[panel];
    document.querySelectorAll(`#panel-${panel} th[data-sort]`).forEach((th) => {
      const on = th.dataset.sort === st.key;
      th.classList.toggle("sorted", on);
      th.setAttribute("aria-sort", on ? (st.dir === "asc" ? "ascending" : "descending") : "none");
      const b = th.querySelector("button");
      b.textContent = b.textContent.replace(/ [▾▴]$/, "") + (on ? (st.dir === "asc" ? " ▴" : " ▾") : "");
    });
  }

  const cellText = (row, key) => {
    const v = value(row, key);
    if (v === null) return "–";
    if (key === "secPerCard") return v.toFixed(1);
    if (key === "retention") return v.toFixed(1) + "%";
    if (key === "minutes") return fmt(Math.round(v));
    return fmt(v);
  };

  // ---- leaderboard ---------------------------------------------------------

  function drawBoard() {
    const body = $("board-rows");
    body.replaceChildren();
    const st = sort.board;
    const rows = sorted(view.people, st.key, st.dir);
    if (!rows.length) {
      const tr = document.createElement("tr");
      const cell = td("Nobody has joined yet.", "empty");
      cell.colSpan = 9;
      tr.append(cell);
      body.append(tr);
    }
    const medals = medalsBy(view.people, "reviews");
    const cols = ["reviews", "minutes", "secPerCard", "streak", "past31", "retention", "added"];
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      tr.className = [p.id === view.me ? "you" : "", p.reviews ? "" : "idle", p.id === chart.compare ? "comparing" : ""].join(" ").trim();
      if (p.id !== view.me) {
        tr.classList.add("pickable");
        tr.title = chart.compare === p.id ? "Stop comparing" : "Compare with " + p.name;
        tr.addEventListener("click", () => toggleCompare(p.id));
      }
      tr.append(
        rankCell(p, i, medals, st, "reviews", "reviews"),
        nameCell(p, medals, st, "reviews", "reviews"),
        ...cols.map((c) => td(cellText(p, c), st.key === c ? "main" : "")),
      );
      body.append(tr);
    });
    markHeaders("board");
    $("metric").textContent = RANKS.find((r) => r.key === st.key).short + " ▾";
  }

  // ---- chart -----------------------------------------------------------------

  function chartMetric() {
    if (chart.show) return chart.show;
    return CHARTABLE.indexOf(sort.board.key) >= 0 ? sort.board.key : "reviews";
  }

  function bucketValue(s, i, m) {
    const v = s.values, r = v.reviews[i], sec = v.seconds[i], a = v.again[i];
    if (m === "reviews") return r;
    if (m === "minutes") return sec / 60;
    if (m === "added") return s.detailed === false ? null : v.added[i];
    if (m === "secPerCard") return r ? sec / r : null;
    return r ? (100 * (r - a)) / r : null;
  }

  function runningValue(s, i, m) {
    let r = 0, sec = 0, a = 0, ad = 0;
    for (let k = 0; k <= i; k++) { r += s.values.reviews[k]; sec += s.values.seconds[k]; a += s.values.again[k]; ad += s.values.added[k]; }
    if (m === "reviews") return r;
    if (m === "minutes") return sec / 60;
    if (m === "added") return s.detailed === false ? null : ad;
    if (m === "secPerCard") return r ? sec / r : null;
    return r ? (100 * (r - a)) / r : null;
  }

  function groupAverage(i, m) {
    let r = 0, sec = 0, a = 0, ad = 0, n = 0, nd = 0;
    view.chart.series.forEach((s) => {
      r += s.values.reviews[i]; sec += s.values.seconds[i]; a += s.values.again[i]; n++;
      if (s.detailed !== false) { ad += s.values.added[i]; nd++; }
    });
    if (!n) return null;
    if (m === "reviews") return r / n;
    if (m === "minutes") return sec / 60 / n;
    if (m === "added") return nd ? ad / nd : null;
    if (m === "secPerCard") return r ? sec / r : null;
    return r ? (100 * (r - a)) / r : null;
  }

  // Everyone is in color unless switched off (or a comparison is on). Eight
  // validated colors; from the ninth person on, lines are dashed and skip
  // blue, which is always yours.
  function highlighted() {
    const all = view.chart.series;
    const on = chart.compare
      ? all.filter((s) => s.id === view.me || s.id === chart.compare)
      : all.filter((s) => !chart.off.has(s.id));
    return on.slice().sort((x, y) => (y.id === view.me) - (x.id === view.me) || x.slot - y.slot);
  }

  function styles(list) {
    const out = {};
    list.forEach((s, k) => {
      const slot = k < 8 ? k + 1 : ((k - 8) % 7) + 2;
      out[s.id] = { color: `var(--s${slot})`, dash: k >= 8 ? "5 3" : "" };
    });
    return out;
  }

  function keyBackground(st) {
    return st.dash ? `repeating-linear-gradient(90deg, ${st.color} 0 4px, transparent 4px 7px)` : st.color;
  }

  function niceStep(raw) {
    if (!(raw > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.append(e);
    return e;
  }

  function linePath(vals, x, y) {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1);
      pen = true;
    });
    return d;
  }

  function legendItem(label, background, id, square) {
    const item = document.createElement(id ? "button" : "span");
    item.className = "legend-item";
    const key = document.createElement("i");
    key.className = square ? "swatch" : "key";
    key.style.background = background;
    const name = document.createElement("span");
    name.textContent = label;
    item.append(key, name);
    if (id) {
      item.type = "button";
      item.title = chart.compare === id ? "Stop comparing" : "Compare with " + label;
      item.addEventListener("click", () => toggleCompare(id));
    }
    return item;
  }

  function drawChart() {
    const c = view.chart;
    const metric = chartMetric();
    const m = METRICS[metric];
    const type = chart.type;
    const n = c.labels.length;
    const isAvg = metric === "secPerCard" || metric === "retention";
    const meSeries = c.series.find((s) => s.id === view.me);
    const on = highlighted();
    const st = styles(on);

    document.querySelectorAll("#chart-types button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.chart === type)));
    $("show-btn").textContent = "";
    const showLabel = document.createElement("span");
    showLabel.className = "muted";
    showLabel.textContent = "Show ";
    $("show-btn").append(showLabel, document.createTextNode(m.label + " ▾"));
    const comparing = !!chart.compare;
    $("hl-btn").hidden = comparing || type === "avg";
    $("compare-chip").hidden = !comparing;
    if (comparing) {
      const other = c.series.find((s) => s.id === chart.compare);
      $("compare-chip").textContent = "You vs " + (other ? other.name : "") + " ✕";
    }
    $("hl-btn").textContent = "";
    const hlLabel = document.createElement("span");
    hlLabel.className = "muted";
    hlLabel.textContent = "Highlight ";
    const count = c.series.filter((s) => !chart.off.has(s.id)).length;
    $("hl-btn").append(hlLabel, document.createTextNode((count === c.series.length ? "Everyone" : count + " of " + c.series.length) + " ▾"));

    $("chart-title").textContent = type === "race"
      ? (isAvg ? m.label + ", running average" : m.label + " so far")
      : type === "avg" ? "Your " + m.label.toLowerCase() + " against the group" : m.label;
    $("chart-caption").textContent = c.caption;

    const trends = type === "trend";
    $("plot").hidden = trends;
    $("legend").hidden = trends;
    $("trends").hidden = !trends;
    $("chart-toggle").hidden = trends;

    if (trends) {
      drawTrends(metric, st);
      drawChartTable([], metric, m);
      return;
    }

    // Values per drawn series.
    let drawn = [];
    let bars = null, avgLine = null;
    if (type === "avg") {
      bars = meSeries ? Array.from({ length: n }, (_, i) => bucketValue(meSeries, i, metric)) : [];
      avgLine = Array.from({ length: n }, (_, i) => groupAverage(i, metric));
    } else {
      const f = type === "race" ? runningValue : bucketValue;
      drawn = c.series.map((s) => ({ s, vals: Array.from({ length: n }, (_, i) => f(s, i, metric)) }));
    }

    const legend = $("legend");
    legend.replaceChildren();
    if (type === "avg") {
      legend.append(legendItem("You", "var(--s1)", null, true), legendItem("Group average", "var(--text)", null, false));
    } else {
      on.forEach((s) => legend.append(legendItem(s.id === view.me ? "You" : s.name, keyBackground(st[s.id]), s.id === view.me ? null : s.id, false)));
      if (on.length < c.series.length) legend.append(legendItem(comparing ? "Everyone else" : "Switched off", "var(--other)", null, false));
    }

    const svg = $("svg");
    svg.replaceChildren();
    const W = Math.max(320, $("plot").clientWidth);
    const H = 220;
    const pad = { l: 46, r: 14, t: 10, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("height", H);
    svg.setAttribute("aria-label", `${$("chart-title").textContent}, ${c.caption}. The table view lists the values.`);

    const all = [];
    drawn.forEach((d) => d.vals.forEach((v) => v != null && all.push(v)));
    (bars || []).forEach((v) => v != null && all.push(v));
    (avgLine || []).forEach((v) => v != null && all.push(v));
    let lo = 0, hi, step;
    if (metric === "retention") {
      lo = all.length ? Math.max(0, Math.floor(Math.min(...all) / 10) * 10 - 10) : 0;
      hi = 100;
      step = niceStep((hi - lo) / 4);
      lo = Math.max(0, hi - Math.ceil((hi - lo) / step) * step);
    } else {
      const max = Math.max(0, ...all);
      step = niceStep(max / 4);
      hi = Math.max(step, Math.ceil(max / step) * step);
    }
    const xLine = (i) => pad.l + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
    const xBar = (i) => pad.l + ((i + 0.5) * iw) / Math.max(1, n);
    const x = type === "avg" ? xBar : xLine;
    const y = (v) => pad.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

    for (let k = 0, v = lo; v <= hi + step / 1000; k++, v = lo + k * step) {
      el("line", { x1: pad.l, x2: pad.l + iw, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }, svg);
      const t = el("text", { x: pad.l - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" }, svg);
      const shown = Number.isInteger(v) ? fmt(v) : v.toFixed(1);
      t.textContent = metric === "retention" ? shown + "%" : shown;
    }
    const every = Math.max(1, Math.ceil(n / 7));
    c.labels.forEach((lab, i) => {
      if (i % every && i !== n - 1) return;
      if (i !== n - 1 && n - 1 - i < every * 0.9) return;
      const t = el("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle", class: "axis" }, svg);
      t.textContent = lab;
    });

    if (type === "avg") {
      const bw = Math.min(24, Math.max(4, (iw / Math.max(1, n)) * 0.6));
      let d = "";
      bars.forEach((v, i) => { if (v) d += `M${(xBar(i) - bw / 2).toFixed(1)} ${y(lo).toFixed(1)}V${y(v).toFixed(1)}h${bw.toFixed(1)}V${y(lo).toFixed(1)}Z`; });
      if (d) el("path", { d, fill: "var(--s1)" }, svg);
      const ad = linePath(avgLine, xBar, y);
      if (ad) el("path", { d: ad, fill: "none", stroke: "var(--text)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    } else {
      drawn.filter((d) => !st[d.s.id]).forEach((d) => {
        const p = linePath(d.vals, xLine, y);
        if (p) el("path", { d: p, fill: "none", stroke: "var(--other)", "stroke-width": 1.5, "stroke-linejoin": "round" }, svg);
      });
      on.slice().reverse().forEach((s) => {
        const d = drawn.find((t) => t.s.id === s.id);
        const p = linePath(d.vals, xLine, y);
        const attrs = { d: p, fill: "none", stroke: st[s.id].color, "stroke-width": s.id === view.me ? 2.5 : 2, "stroke-linejoin": "round", "stroke-linecap": "round" };
        if (st[s.id].dash) attrs["stroke-dasharray"] = st[s.id].dash;
        if (p) el("path", attrs, svg);
      });
    }

    const cross = el("line", { y1: pad.t, y2: pad.t + ih, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden" }, svg);
    const hit = el("rect", { x: pad.l - 10, y: 0, width: iw + 20, height: H, fill: "transparent" }, svg);
    const show = (i) => {
      hover = i;
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("visibility", "visible");
      const tip = $("tip");
      tip.replaceChildren();
      const when = document.createElement("div");
      when.className = "when";
      when.textContent = (c.unit === "week" ? "Week of " : "") + c.labels[i];
      tip.append(when);
      const rows = type === "avg"
        ? [{ bg: "var(--s1)", v: bars[i], name: "You" }, { bg: "var(--text)", v: avgLine[i], name: "Group average" }]
        : on.map((s) => ({ bg: keyBackground(st[s.id]), v: drawn.find((t) => t.s.id === s.id).vals[i], name: s.id === view.me ? "You" : s.name }))
            .sort((a, b) => (b.v ?? -1) - (a.v ?? -1));
      rows.forEach((r) => {
        const row = document.createElement("div");
        row.className = "r";
        const key = document.createElement("i");
        key.className = "key";
        key.style.background = r.bg;
        const val = document.createElement("b");
        val.textContent = r.v == null ? "–" : m.fmt(r.v);
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = r.name;
        row.append(key, val, who);
        tip.append(row);
      });
      tip.hidden = false;
      const left = x(i) + 14 + tip.offsetWidth > W ? x(i) - 14 - tip.offsetWidth : x(i) + 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = pad.t + "px";
    };
    const hide = () => {
      hover = null;
      cross.setAttribute("visibility", "hidden");
      $("tip").hidden = true;
    };
    hit.addEventListener("pointermove", (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const i = type === "avg" ? Math.floor(((px - pad.l) / (iw || 1)) * n) : Math.round(((px - pad.l) / (iw || 1)) * (n - 1));
      show(Math.max(0, Math.min(n - 1, i)));
    });
    hit.addEventListener("pointerleave", hide);
    svg.onkeydown = (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      const i = hover == null ? n - 1 : hover + (ev.key === "ArrowRight" ? 1 : -1);
      show(Math.max(0, Math.min(n - 1, i)));
    };
    svg.onblur = hide;

    drawChartTable(type === "avg" ? null : on, metric, m, drawn, bars, avgLine);
  }

  function drawTrends(metric, st) {
    const c = view.chart;
    const box = $("trends");
    box.replaceChildren();
    const n = c.labels.length;
    const total = (s) => {
      if (metric === "secPerCard" || metric === "retention") return runningValue(s, n - 1, metric);
      if (metric === "added" && s.detailed === false) return null;
      return Array.from({ length: n }, (_, i) => bucketValue(s, i, metric) || 0).reduce((a, b) => a + b, 0);
    };
    const people = c.series.map((s) => ({ s, t: total(s) }))
      .sort((a, b) => (a.t === null) - (b.t === null) || (metric === "secPerCard" ? a.t - b.t : b.t - a.t));
    people.forEach(({ s, t }) => {
      const vals = Array.from({ length: n }, (_, i) => bucketValue(s, i, metric));
      const known = vals.filter((v) => v != null);
      const mx = Math.max(1e-9, ...known);
      const mn = metric === "retention" && known.length ? Math.min(...known) : 0;
      const row = document.createElement(s.id === view.me ? "div" : "button");
      row.className = "trend-row" + (s.id === view.me ? " you" : "") + (s.id === chart.compare ? " comparing" : "");
      if (s.id !== view.me) {
        row.type = "button";
        row.title = chart.compare === s.id ? "Stop comparing" : "Compare with " + s.name;
        row.addEventListener("click", () => toggleCompare(s.id));
      }
      const name = document.createElement("span");
      name.className = "who";
      name.textContent = s.id === view.me ? s.name + " (you)" : s.name;
      const svg = el("svg", { width: 150, height: 24, viewBox: "0 0 150 24", "aria-hidden": "true" });
      const d = linePath(vals, (i) => 2 + (n <= 1 ? 73 : (i * 146) / (n - 1)), (v) => 22 - ((v - mn) / (mx - mn || 1)) * 20);
      const s2 = st[s.id];
      if (d) {
        const attrs = { d, fill: "none", stroke: s2 ? s2.color : "var(--other)", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" };
        if (s2 && s2.dash) attrs["stroke-dasharray"] = s2.dash;
        el("path", attrs, svg);
      }
      const num = document.createElement("span");
      num.className = "total";
      num.textContent = t == null ? "–" : METRICS[metric].fmt(t);
      row.append(name, svg, num);
      box.append(row);
    });
  }

  function drawChartTable(list, metric, m, drawn, bars, avgLine) {
    const c = view.chart;
    const box = $("chart-table");
    box.replaceChildren();
    if (!drawn && !bars) return;
    const table = document.createElement("table");
    const head = document.createElement("tr");
    const th0 = document.createElement("th");
    th0.textContent = c.unit === "day" ? "Day" : c.unit === "week" ? "Week of" : "Month";
    head.append(th0);
    const cols = bars
      ? [{ name: "You", vals: bars }, { name: "Group average", vals: avgLine }]
      : list.map((s) => ({ name: s.id === view.me ? "You" : s.name, vals: drawn.find((t) => t.s.id === s.id).vals }));
    cols.forEach((col) => {
      const th = document.createElement("th");
      th.textContent = col.name;
      head.append(th);
    });
    table.append(head);
    c.labels.forEach((lab, i) => {
      const tr = document.createElement("tr");
      const t0 = document.createElement("td");
      t0.textContent = lab;
      tr.append(t0);
      cols.forEach((col) => {
        const cell = document.createElement("td");
        const v = col.vals[i];
        cell.textContent = v == null ? "–" : m.fmt(v);
        tr.append(cell);
      });
      table.append(tr);
    });
    box.append(table);
  }

  function toggleChartTable() {
    const box = $("chart-table");
    box.hidden = !box.hidden;
    $("chart-toggle").textContent = box.hidden ? "Show as table" : "Hide table";
  }

  function toggleCompare(id) {
    chart.compare = chart.compare === id ? null : id;
    drawBoard();
    drawChart();
  }

  function endCompare() {
    chart.compare = null;
    drawBoard();
    drawChart();
  }

  // ---- breakdown ---------------------------------------------------------

  function drawBreakdown() {
    const body = $("breakdown-rows");
    body.replaceChildren();
    const st = sort.breakdown;
    const rows = sorted(view.people, st.key, st.dir);
    const medals = medalsBy(view.people, "reviews");
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      tr.className = [p.id === view.me ? "you" : "", p.reviews ? "" : "idle"].join(" ").trim();
      tr.append(
        rankCell(p, i, medals, st, "reviews", "reviews"),
        nameCell(p, medals, st, "reviews", "reviews"),
        td(fmt(p.reviews), st.key === "reviews" ? "main" : ""),
        ...GROUPS.map((g) => td(cellText(p, g.key), st.key === g.key ? "main" : "")),
        td(cellText(p, "added"), st.key === "added" ? "main" : ""),
      );
      body.append(tr);
    });
    markHeaders("breakdown");

    // One stacked bar per person, lengths on a shared scale.
    $("mix-caption").textContent = view.range.caption;
    const bars = $("bars");
    bars.replaceChildren();
    const max = Math.max(1, ...rows.map((p) => p.reviews));
    rows.forEach((p) => {
      const row = document.createElement("div");
      row.className = "bar-row" + (p.id === view.me ? " you" : "") + (p.reviews ? "" : " idle");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = p.id === view.me ? p.name + " (you)" : p.name;
      const track = document.createElement("div");
      track.className = "track";
      track.style.width = Math.max(0, (p.reviews / max) * 100) + "%";
      if (p.detailed === false && p.reviews) {
        const seg = document.createElement("span");
        seg.className = "seg-bar unknown";
        seg.style.flex = "1 1 0";
        seg.title = "This person's add-on doesn't send this breakdown yet.";
        track.append(seg);
      }
      GROUPS.forEach((g) => {
        const nn = p.detailed === false ? 0 : p[g.key];
        if (!nn) return;
        const seg = document.createElement("span");
        seg.className = "seg-bar " + g.cls;
        seg.style.flex = nn + " 1 0";
        seg.tabIndex = 0;
        const pct = p.reviews ? Math.round((100 * nn) / p.reviews) : 0;
        seg.setAttribute("aria-label", `${p.name}, ${g.label}: ${fmt(nn)} (${pct}%)`);
        const showTip = () => {
          const tip = $("mix-tip");
          tip.replaceChildren();
          const v = document.createElement("b");
          v.textContent = fmt(nn) + " ";
          const l = document.createElement("span");
          l.className = "who";
          l.textContent = `${g.label}, ${pct}% of ${p.id === view.me ? "your" : p.name + "'s"} answers`;
          tip.append(v, l);
          tip.hidden = false;
          const box = seg.getBoundingClientRect(), host = bars.parentElement.getBoundingClientRect();
          tip.style.left = Math.min(host.width - 240, Math.max(0, box.left - host.left)) + "px";
          tip.style.top = box.bottom - host.top + 6 + "px";
        };
        seg.addEventListener("pointerenter", showTip);
        seg.addEventListener("focus", showTip);
        seg.addEventListener("pointerleave", () => ($("mix-tip").hidden = true));
        seg.addEventListener("blur", () => ($("mix-tip").hidden = true));
        track.append(seg);
      });
      const total = document.createElement("span");
      total.className = "total";
      total.textContent = fmt(p.reviews);
      row.append(who, track, total);
      bars.append(row);
    });
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Nobody has joined yet.";
      bars.append(empty);
    }
  }

  // ---- league ------------------------------------------------------------

  function drawLeague() {
    const lg = view.league;
    const body = $("league-rows");
    body.replaceChildren();
    if (!lg.season) {
      $("season-title").textContent = "League";
      $("season-dates").textContent = "The first season starts on " + lg.startsOn + ".";
      $("season-left").textContent = "";
      return;
    }
    $("season-title").textContent = "Season " + lg.season;
    $("season-dates").textContent = lg.start + " to " + lg.end;
    $("season-left").textContent = lg.daysLeft === 1 ? "Last day" : lg.daysLeft + " days left";
    const st = sort.league;
    const anyMedals = lg.rows.some((r) => MEDAL.some((m) => (r.medals || {})[m]));
    $("medals-th").hidden = !anyMedals;
    const medals = medalsBy(lg.rows, "xp");
    sorted(lg.rows, st.key, st.dir).forEach((r, i) => {
      const tr = document.createElement("tr");
      tr.className = [r.id === view.me ? "you" : "", r.reviews ? "" : "idle"].join(" ").trim();
      const medalCell = td("");
      medalCell.hidden = !anyMedals;
      MEDAL.forEach((m) => {
        const count = (r.medals || {})[m];
        if (count) {
          const pill = document.createElement("span");
          pill.className = "medal " + m;
          pill.textContent = m[0].toUpperCase() + m.slice(1) + " " + count;
          medalCell.append(pill);
        }
      });
      tr.append(
        rankCell(r, i, medals, st, "xp", "XP"),
        nameCell(r, medals, st, "xp", "XP"),
        td(fmt(r.xp), st.key === "xp" ? "main" : ""),
        td(fmt(r.reviews), st.key === "reviews" ? "main" : ""),
        td(fmt(Math.round(r.minutes)), st.key === "minutes" ? "main" : ""),
        td(r.reviews ? r.retention.toFixed(1) + "%" : "–", st.key === "retention" ? "main" : ""),
        td(`${r.daysLearned} of ${r.daysElapsed}`, st.key === "daysPercent" ? "main" : ""),
        medalCell,
      );
      body.append(tr);
    });
    markHeaders("league");
  }

  // ---- remembered view -----------------------------------------------------

  function savePrefs() {
    const prefs = { tab: currentTab, sort, chart: chart.type, show: chart.show, off: [...chart.off] };
    pycmd("prefs:" + JSON.stringify(prefs));
  }

  function applyPrefs(prefs, pinnedMetric) {
    if (["board", "breakdown", "league"].indexOf(prefs.tab) >= 0) currentTab = prefs.tab;
    const okSort = (st) => st && typeof st.key === "string" && (st.dir === "asc" || st.dir === "desc");
    if (prefs.sort) ["board", "breakdown", "league"].forEach((k) => { if (okSort(prefs.sort[k])) sort[k] = prefs.sort[k]; });
    else if (pinnedMetric && RANKS.some((r) => r.key === pinnedMetric)) sort.board = { key: pinnedMetric, dir: defaultDir(pinnedMetric) };
    if (["day", "race", "avg", "trend"].indexOf(prefs.chart) >= 0) chart.type = prefs.chart;
    if (prefs.show === null || CHARTABLE.indexOf(prefs.show) >= 0) chart.show = prefs.show ?? null;
    if (Array.isArray(prefs.off)) chart.off = new Set(prefs.off.filter((x) => typeof x === "string"));
  }

  // ---- shell -------------------------------------------------------------

  function render(v) {
    $("refresh").classList.remove("spinning");
    const first = view === null;
    view = v;
    if (first) applyPrefs(v.prefs || {}, v.pinned ? v.pinnedMetric : null);
    $("status").classList.remove("error");
    $("status").removeAttribute("title");
    $("status").textContent = v.updated ? "Updated " + v.updated : "";
    document.querySelectorAll("#ranges button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.key === v.range.key)));
    $("custom").classList.toggle("open", v.range.key === "custom");
    $("from").value = v.range.from;
    $("to").value = v.range.to;
    $("from").classList.remove("bad");
    $("to").classList.remove("bad");
    $("caption").textContent = v.range.caption;
    $("announce").hidden = !v.announcement;
    $("announce").textContent = v.announcement;
    const pinLabel = v.pinned ? "Unpin from home screen" : "Pin to home screen";
    $("pin").setAttribute("aria-pressed", String(v.pinned));
    $("pin").setAttribute("aria-label", pinLabel);
    $("pin").title = pinLabel;
    if (first) tab(currentTab, true);
    drawBoard();
    drawBreakdown();
    drawLeague();
    if (currentTab === "board") drawChart();
  }

  function loading(on) {
    $("refresh").classList.toggle("spinning", on);
    if (on && $("status").classList.contains("error")) {
      $("status").classList.remove("error");
      $("status").textContent = "";
    }
  }

  function error(message) {
    $("refresh").classList.remove("spinning");
    $("status").textContent = "Couldn't refresh";
    $("status").title = message;
    $("status").classList.add("error");
  }

  function tab(name, quiet) {
    currentTab = name;
    ["board", "breakdown", "league"].forEach((t) => {
      $("tab-" + t).setAttribute("aria-selected", String(t === name));
      $("panel-" + t).hidden = t !== name;
    });
    $("range-block").hidden = name === "league";
    $("rankby").hidden = name !== "board";
    if (name === "board" && view) drawChart();
    if (!quiet) savePrefs();
  }

  function sortBy(panel, key, fromHeading) {
    const st = sort[panel];
    sort[panel] = fromHeading && st.key === key ? { key, dir: st.dir === "desc" ? "asc" : "desc" } : { key, dir: defaultDir(key) };
    if (!view) return;
    if (panel === "board") {
      drawBoard();
      drawChart();
      if (view.pinned) pycmd("pin:on:" + key);
    } else if (panel === "breakdown") {
      drawBreakdown();
    } else {
      drawLeague();
    }
    savePrefs();
  }

  function setChartType(type) {
    chart.type = type;
    hover = null;
    drawChart();
    savePrefs();
  }

  function togglePin() {
    if (!view) return;
    pycmd((view.pinned ? "pin:off:" : "pin:on:") + sort.board.key);
  }

  function applyCustom() {
    const a = parseDate($("from").value), b = parseDate($("to").value);
    $("from").classList.toggle("bad", !a);
    $("to").classList.toggle("bad", !b);
    if (!a || !b) return;
    closePops();
    pycmd("range:custom:" + iso(a) + ":" + iso(b));
  }

  // ---- menus -------------------------------------------------------------

  // items: [{ label, checked, role, onPick, keepOpen }]
  function openMenuFor(anchor, label, items) {
    const menu = $("menu");
    if (!menu.hidden && menuAnchor === anchor) { closePops(); return; }
    closePops();
    menu.replaceChildren();
    menu.setAttribute("aria-label", label);
    items.forEach((it) => {
      const b = document.createElement("button");
      b.setAttribute("role", it.role || "menuitemradio");
      b.setAttribute("aria-checked", String(!!it.checked));
      if (it.role === "menuitemcheckbox") {
        const box = document.createElement("i");
        box.className = "checkbox";
        const name = document.createElement("span");
        name.textContent = it.label;
        b.append(box, name);
        if (it.key) {
          const key = document.createElement("i");
          key.className = "key";
          key.style.background = it.key;
          b.append(key);
        }
      } else {
        b.textContent = it.label;
      }
      if (it.separator) b.classList.add("separated");
      b.onclick = () => {
        it.onPick();
        if (it.keepOpen) {
          const keep = menuAnchor;
          const items2 = it.refresh();
          menuAnchor = null;
          openMenuFor(keep, label, items2);
          const again = [...$("menu").querySelectorAll("button")][items.indexOf(it)];
          if (again) again.focus();
        } else {
          closePops();
          anchor.focus();
        }
      };
      menu.append(b);
    });
    anchor.setAttribute("aria-expanded", "true");
    menuAnchor = anchor;
    place(menu, anchor);
    (menu.querySelector('[aria-checked="true"]') || menu.firstChild).focus();
  }

  function openMenu() {
    openMenuFor($("metric"), "Rank by", RANKS.map((r) => ({ label: r.label, checked: r.key === sort.board.key, onPick: () => sortBy("board", r.key) })));
  }

  function openShow() {
    const items = [{ label: "Same as the table", checked: chart.show === null, onPick: () => { chart.show = null; drawChart(); savePrefs(); } }]
      .concat(CHARTABLE.map((k, i) => ({ label: METRICS[k].label, checked: chart.show === k, separator: i === 0, onPick: () => { chart.show = k; drawChart(); savePrefs(); } })));
    openMenuFor($("show-btn"), "Show", items);
  }

  function highlightItems() {
    const on = highlighted();
    const st = styles(on);
    return view.chart.series.map((s) => ({
      label: s.id === view.me ? s.name + " (you)" : s.name,
      role: "menuitemcheckbox",
      checked: !chart.off.has(s.id),
      key: st[s.id] ? keyBackground(st[s.id]) : "transparent",
      keepOpen: true,
      refresh: highlightItems,
      onPick: () => {
        if (chart.off.has(s.id)) chart.off.delete(s.id); else chart.off.add(s.id);
        drawChart();
        savePrefs();
      },
    }));
  }

  function openHighlight() {
    openMenuFor($("hl-btn"), "Highlight", highlightItems());
  }

  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  let calField = null, calMonth = null;

  function pad(n) { return String(n).padStart(2, "0"); }
  function iso(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function parseDate(text) {
    const m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$/.exec(text || "");
    if (!m) return null;
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    return d.getMonth() === +m[2] - 1 && d.getDate() === +m[3] ? d : null;
  }
  function todayDate() { return parseDate(view ? view.range.today : "") || new Date(); }

  function place(pop, anchor) {
    const host = document.querySelector(".wrap").getBoundingClientRect();
    const box = anchor.getBoundingClientRect();
    pop.hidden = false;
    const left = Math.min(box.left - host.left, host.width - pop.offsetWidth - 8);
    pop.style.left = Math.max(8, left) + "px";
    pop.style.top = box.bottom - host.top + 6 + "px";
  }

  function closePops() {
    $("cal").hidden = true;
    $("menu").hidden = true;
    document.querySelectorAll("[aria-haspopup=menu]").forEach((b) => b.setAttribute("aria-expanded", "false"));
    calField = null;
    menuAnchor = null;
  }

  function openCal(field) {
    $("menu").hidden = true;
    calField = field;
    const d = parseDate($(field).value) || todayDate();
    calMonth = new Date(d.getFullYear(), d.getMonth(), 1);
    drawCal();
    place($("cal"), $(field));
  }

  function drawCal() {
    const cal = $("cal");
    cal.replaceChildren();
    const today = todayDate();
    const picked = parseDate($(calField).value);
    const head = document.createElement("div");
    head.className = "cal-head";
    const prev = document.createElement("button");
    prev.className = "cal-nav"; prev.textContent = "‹"; prev.setAttribute("aria-label", "Previous month");
    prev.onclick = () => { calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1); drawCal(); };
    const next = document.createElement("button");
    next.className = "cal-nav"; next.textContent = "›"; next.setAttribute("aria-label", "Next month");
    next.disabled = calMonth.getFullYear() * 12 + calMonth.getMonth() >= today.getFullYear() * 12 + today.getMonth();
    next.onclick = () => { calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1); drawCal(); };
    const title = document.createElement("b");
    title.textContent = MONTHS[calMonth.getMonth()] + " " + calMonth.getFullYear();
    head.append(prev, title, next);
    const grid = document.createElement("div");
    grid.className = "cal-grid";
    ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].forEach((d) => {
      const el = document.createElement("span"); el.className = "dow"; el.textContent = d; grid.append(el);
    });
    const lead = (calMonth.getDay() + 6) % 7;  // weeks start on Monday
    for (let i = 0; i < lead; i++) grid.append(document.createElement("span"));
    const days = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0).getDate();
    for (let day = 1; day <= days; day++) {
      const d = new Date(calMonth.getFullYear(), calMonth.getMonth(), day);
      const b = document.createElement("button");
      b.textContent = day;
      b.setAttribute("aria-label", MONTHS[d.getMonth()] + " " + day + ", " + d.getFullYear());
      if (iso(d) === iso(today)) b.classList.add("today");
      if (picked && iso(d) === iso(picked)) b.classList.add("picked");
      b.disabled = d > today;
      b.onclick = () => {
        const field = calField;
        $(field).value = iso(d);
        $(field).classList.remove("bad");
        closePops();
        $(field).focus({ preventScroll: true });
      };
      grid.append(b);
    }
    cal.append(head, grid);
  }

  ["from", "to"].forEach((id) => {
    $(id).addEventListener("click", () => openCal(id));
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") applyCustom();
      if (e.key === "ArrowDown") { e.preventDefault(); openCal(id); const b = $("cal").querySelector("button.picked, .cal-grid button:not(:disabled)"); if (b) b.focus(); }
    });
  });
  // Close on Escape or a click elsewhere; neither touches anything outside the page.
  document.addEventListener("mousedown", (e) => {
    const inside = (id) => $(id).contains(e.target);
    const onAnchor = menuAnchor && menuAnchor.contains(e.target);
    if (!inside("cal") && !inside("menu") && e.target !== $("from") && e.target !== $("to") && !onAnchor) closePops();
  });
  ["cal", "menu"].forEach((id) => $(id).addEventListener("keydown", (e) => {
    if (e.key === "Escape") { const back = calField ? $(calField) : menuAnchor; closePops(); if (back) back.focus(); }
    if (id === "menu" && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const items = [...$("menu").querySelectorAll("button")];
      const i = items.indexOf(document.activeElement);
      const next = items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
      if (next) next.focus();
    }
  }));

  document.querySelectorAll("#ranges button").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.key === "custom") {
        document.querySelectorAll("#ranges button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        $("custom").classList.add("open");
        return;
      }
      pycmd("range:" + b.dataset.key);
    });
  });
  document.querySelectorAll("#chart-types button").forEach((b) => {
    b.addEventListener("click", () => setChartType(b.dataset.chart));
  });
  ["board", "breakdown", "league"].forEach((panel) => {
    document.querySelectorAll(`#panel-${panel} th[data-sort] button`).forEach((btn) => {
      btn.addEventListener("click", () => sortBy(panel, btn.parentElement.dataset.sort, true));
    });
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => view && currentTab === "board" && drawChart(), 100);
  });

  return { render, loading, error, tab, sortBy, applyCustom, togglePin, toggleChartTable, openMenu, openShow, openHighlight, endCompare };
})();
</script>
"""
