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

    mw.taskman.run_in_background(lambda: client.call(url, payload), done)


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
        self.range = ("today", None, None)
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
            self.load()
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
                self.web.eval(f"lb.error({json.dumps(str(error))})")
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
        pinned = bool(cfg.get("home_pin")) and list(cfg.get("home_range") or []) == list(self.range)
        fetched = (_cache or {}).get("fetched")
        view = {
            "me": st["me"],
            "announcement": str(st["settings"].get("announcement") or ""),
            "updated": fetched.strftime("%I:%M %p").lstrip("0") if fetched else "",
            "pinned": pinned,
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
        return start, today, f"This week: {_short(start)} to {_short(start + 6 * DAY)}"
    if key == "month":
        return today.replace(day=1), today, today.strftime("%B %Y")
    if key == "year":
        return today.replace(month=1, day=1), today, today.strftime("%Y")
    if key == "all":
        return None, None, "All time, including reviews from before joining"
    if key == "custom":
        start, end = _parse(a) or today, _parse(b) or today
        if start > end:
            start, end = end, start
        end = min(end, today)
        start = min(start, end)
        return start, end, f"{_short(start)} to {_short(end)}"
    return today, today, f"Today, {_short(today)}"


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
    per = {"day": "By day", "week": "By week", "month": "By month"}[unit]
    return f"{per}, {_short(start)} to {_short(end)}"


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
  }
  button, select, input { font: inherit; color: var(--text); }
  .wrap { padding: 0 22px 18px; display: flex; flex-direction: column; gap: 14px; min-height: 100vh; box-sizing: border-box; }
  .tabs { display: flex; align-items: flex-end; gap: 4px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .tab { height: 44px; padding: 0 14px; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; color: var(--muted); cursor: pointer; }
  .tab[aria-selected="true"] { border-bottom-color: var(--accent); color: var(--text); font-weight: 650; }
  .spacer { flex: 1; }
  .status { align-self: center; color: var(--muted); font-size: 12px; margin-right: 8px; }
  .btn { height: 28px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .btn[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .tabs .btn { align-self: center; margin-left: 6px; }
  .seg { display: flex; padding: 3px; gap: 2px; border-radius: 8px; background: var(--track); }
  .seg button { flex: 1 1 0; height: 30px; border: 0; border-radius: 6px; background: none; color: var(--muted); cursor: pointer; }
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
  .announce { margin: 0; padding: 10px 12px; border-radius: 8px; background: var(--surface); color: var(--muted); }
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
  .seg-bar:last-child { border-radius: 0 4px 4px 0; }
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
    <span class="status" id="status">Loading…</span>
    <button class="btn" id="pin" aria-pressed="false" onclick="lb.togglePin()">Pin to home screen</button>
    <button class="btn" onclick="pycmd('refresh')">Refresh</button>
  </div>

  <section id="range-block" style="display:flex;flex-direction:column;gap:14px">
      <div class="seg" role="group" aria-label="Date range" id="ranges">
        <button data-key="today">Today</button>
        <button data-key="week">Week</button>
        <button data-key="month">Month</button>
        <button data-key="year">Year</button>
        <button data-key="all">All time</button>
        <button data-key="custom">Custom…</button>
      </div>
      <div class="row custom" id="custom">
        <label for="from">From</label><input type="date" id="from">
        <label for="to">To</label><input type="date" id="to">
        <button class="btn" onclick="lb.applyCustom()">Show</button>
      </div>
      <div class="row">
        <span class="caption" id="caption"></span>
        <span class="row" id="rankby">
        <label for="metric">Rank by</label>
        <select id="metric" onchange="lb.sortBy('board', this.value)">
          <option value="reviews">Reviews</option>
          <option value="minutes">Minutes</option>
          <option value="secPerCard">Seconds per card (fastest)</option>
          <option value="streak">Streak</option>
          <option value="past31">Past 31 days</option>
          <option value="retention">Retention</option>
        </select>
        </span>
      </div>
  </section>

  <section id="panel-board">
    <div style="display:flex;flex-direction:column;gap:14px">
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="reviews"><button>Reviews</button></th>
          <th data-sort="minutes"><button>Minutes</button></th>
          <th data-sort="secPerCard"><button>Sec/card</button></th>
          <th data-sort="streak"><button>Streak</button></th>
          <th data-sort="past31"><button>Past 31 days</button></th>
          <th data-sort="retention"><button>Retention</button></th>
        </tr></thead>
        <tbody id="board-rows"></tbody>
      </table>

      <section class="chart" aria-labelledby="chart-title">
        <div class="chart-head">
          <h3 id="chart-title"></h3>
          <span class="caption" id="chart-caption"></span>
          <button class="linkbtn" id="chart-toggle" onclick="lb.toggleChartTable()">Show as table</button>
        </div>
        <div class="legend" id="legend"></div>
        <div class="plot" id="plot">
          <svg id="svg" role="img" tabindex="0" aria-describedby="chart-help"></svg>
          <div class="tip" id="tip" hidden></div>
        </div>
        <p class="note" id="chart-help">Hover over the chart, or focus it and use the arrow keys, to see each day's numbers.</p>
        <div class="chart-table" id="chart-table" hidden></div>
      </section>
    </div>
  </section>

  <section id="panel-breakdown" hidden>
    <div style="display:flex;flex-direction:column;gap:14px">
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="reviews"><button>Reviews</button></th>
          <th data-sort="learn"><button>Learning</button></th>
          <th data-sort="young"><button>Young</button></th>
          <th data-sort="mature"><button>Mature</button></th>
          <th data-sort="relearn"><button>Relearning</button></th>
          <th data-sort="added"><button>Added</button></th>
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
      <p class="note">These match Anki's Reviews graph. Learning is a new card's first steps, Young and Mature are reviews of cards with intervals under or over 21 days, and Relearning is a forgotten card going back through its steps. Added counts cards created.</p>
    </div>
  </section>

  <section id="panel-league" hidden>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="season"><h2 id="season-title"></h2><span class="caption" id="season-dates"></span><b id="season-left"></b></div>
      <table class="lb">
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="xp"><button>XP</button></th>
          <th data-sort="reviews"><button>Reviews</button></th>
          <th data-sort="minutes"><button>Minutes</button></th>
          <th data-sort="retention"><button>Retention</button></th>
          <th data-sort="daysPercent"><button>Days</button></th>
          <th>Medals</th>
        </tr></thead>
        <tbody id="league-rows"></tbody>
      </table>
      <p class="note">XP is days studied % × (6 × minutes + 2 × reviews × retention bonus). A day counts as studied with at least 5 minutes. The top three when a season ends get gold, silver and bronze.</p>
    </div>
  </section>

  <span class="spacer"></span>
  <p class="announce" id="announce" hidden></p>
</div>
<script>
const lb = (() => {
  let view = null;
  let hover = null;
  const sort = { board: "reviews", breakdown: "reviews", league: "xp" };
  const GROUPS = [
    { key: "learn", label: "Learning", cls: "g-learn" },
    { key: "young", label: "Young", cls: "g-young" },
    { key: "mature", label: "Mature", cls: "g-mature" },
    { key: "relearn", label: "Relearning", cls: "g-relearn" },
  ];
  const SLOTS = 8;
  const NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n || 0).toLocaleString();
  const METRICS = {
    reviews: { label: "Reviews", fmt: (v) => fmt(v) },
    minutes: { label: "Minutes", fmt: (v) => fmt(Math.round(v)) },
    secPerCard: { label: "Seconds per card", fmt: (v) => v.toFixed(1) + "s" },
    retention: { label: "Retention", fmt: (v) => v.toFixed(1) + "%" },
  };

  function td(text, cls) {
    const el = document.createElement("td");
    if (cls) el.className = cls;
    el.textContent = text;
    return el;
  }

  function sorted(rows, key) {
    if (key === "secPerCard") {
      // Fastest first; people with no reviews in the range go last.
      return rows.slice().sort((a, b) => ((a.reviews ? a[key] : Infinity) - (b.reviews ? b[key] : Infinity)) || a.name.localeCompare(b.name));
    }
    return rows.slice().sort((a, b) => (b[key] - a[key]) || a.name.localeCompare(b.name));
  }

  function markHeaders(panel, key) {
    document.querySelectorAll(`#panel-${panel} th[data-sort]`).forEach((th) => {
      const on = th.dataset.sort === key;
      th.classList.toggle("sorted", on);
      const b = th.querySelector("button");
      b.textContent = b.textContent.replace(/ [▾▴]$/, "") + (on ? (key === "secPerCard" ? " ▴" : " ▾") : "");
    });
  }

  function drawBoard() {
    const body = $("board-rows");
    body.replaceChildren();
    const rows = sorted(view.people, sort.board);
    if (!rows.length) {
      const tr = document.createElement("tr");
      const cell = td("Nobody has joined yet.", "empty");
      cell.colSpan = 8;
      tr.append(cell);
      body.append(tr);
    }
    const main = (k) => (sort.board === k ? "main" : "");
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      if (p.id === view.me) tr.className = "you";
      tr.append(
        td(i + 1, "rank l"), td(p.id === view.me ? p.name + " (you)" : p.name, "name l"),
        td(fmt(p.reviews), main("reviews")),
        td(fmt(Math.round(p.minutes)), main("minutes")),
        td(p.reviews ? p.secPerCard.toFixed(1) : "–", main("secPerCard")),
        td(fmt(p.streak), main("streak")),
        td(fmt(p.past31), main("past31")),
        td(p.reviews ? p.retention.toFixed(1) + "%" : "–", main("retention")),
      );
      body.append(tr);
    });
    markHeaders("board", sort.board);
    $("metric").value = sort.board;
  }

  // ---- chart -------------------------------------------------------------

  function chartMetric() {
    return METRICS[sort.board] ? sort.board : "reviews";
  }

  function chartSeries() {
    return view.chart.series.filter((s) => s.slot < SLOTS);
  }

  function color(s) {
    return `var(--s${s.slot + 1})`;
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

  function drawChart() {
    const c = view.chart;
    const metric = chartMetric();
    const m = METRICS[metric];
    const series = chartSeries();
    const n = c.labels.length;
    const direct = series.length <= 4;
    $("chart-title").textContent = m.label + (sort.board !== metric ? " (this view has no daily chart, so it shows reviews)" : "");
    $("chart-caption").textContent = c.caption;

    const legend = $("legend");
    legend.replaceChildren();
    if (series.length > 1) {
      series.forEach((s) => {
        const item = document.createElement("span");
        const key = document.createElement("i");
        key.className = "key";
        key.style.background = color(s);
        const name = document.createElement("span");
        name.textContent = s.id === view.me ? s.name + " (you)" : s.name;
        item.append(key, name);
        legend.append(item);
      });
    }
    if (c.series.length > SLOTS) {
      const more = document.createElement("span");
      more.textContent = `The chart shows the first ${SLOTS} people who joined; everyone is in the table above.`;
      legend.append(more);
    }

    const svg = $("svg");
    svg.replaceChildren();
    const W = Math.max(320, $("plot").clientWidth);
    const H = 220;
    const pad = { l: 46, r: direct ? 96 : 14, t: 10, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("height", H);
    svg.setAttribute("aria-label", `${m.label} ${c.caption.toLowerCase()} for ${series.length} people. The table view lists every value.`);

    const all = [];
    series.forEach((s) => s.values[metric].forEach((v) => v != null && all.push(v)));
    // Round ticks: pick a nice step, then end the axis on a whole step.
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
    const x = (i) => pad.l + (n <= 1 ? iw / 2 : (i * iw) / (n - 1));
    const y = (v) => pad.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

    for (let k = 0, v = lo; v <= hi + step / 1000; k++, v = lo + k * step) {
      el("line", { x1: pad.l, x2: pad.l + iw, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }, svg);
      const t = el("text", { x: pad.l - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" }, svg);
      const shown = Number.isInteger(v) ? fmt(v) : v.toFixed(1);
      t.textContent = metric === "retention" ? shown + "%" : shown;
    }
    // About seven date labels; the last one always shows, so the one before
    // it is dropped when they'd crowd each other.
    const every = Math.max(1, Math.ceil(n / 7));
    c.labels.forEach((lab, i) => {
      if (i % every && i !== n - 1) return;
      if (i !== n - 1 && n - 1 - i < every * 0.9) return;
      const t = el("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle", class: "axis" }, svg);
      t.textContent = lab;
    });

    const ends = [];
    series.forEach((s) => {
      const vals = s.values[metric];
      let d = "", pen = false;
      vals.forEach((v, i) => {
        if (v == null) { pen = false; return; }
        d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1);
        pen = true;
      });
      if (d) el("path", { d, fill: "none", stroke: color(s), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      for (let i = vals.length - 1; i >= 0; i--) {
        if (vals[i] != null) { ends.push({ s, i, v: vals[i] }); break; }
      }
    });
    ends.forEach((e) => el("circle", { cx: x(e.i), cy: y(e.v), r: 4, fill: color(e.s), stroke: "var(--bg)", "stroke-width": 2 }, svg));
    if (direct) {
      // Stack labels 13px apart, then slide the stack back inside the plot.
      const gap = 13, top = pad.t, bottom = pad.t + ih;
      const placed = ends.map((e) => ({ e, ty: y(e.v) })).sort((a, b) => a.ty - b.ty);
      for (let k = 1; k < placed.length; k++) placed[k].ty = Math.max(placed[k].ty, placed[k - 1].ty + gap);
      const over = placed.length ? placed[placed.length - 1].ty - bottom : 0;
      if (over > 0) placed.forEach((p) => (p.ty -= over));
      for (let k = 0; k < placed.length; k++) placed[k].ty = Math.max(placed[k].ty, k ? placed[k - 1].ty + gap : top);
      placed.forEach(({ e, ty }) => {
        const t = el("text", { x: x(e.i) + 10, y: ty + 4, class: "dlabel" }, svg);
        t.textContent = e.s.id === view.me ? "You" : e.s.name;
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
      series
        .map((s) => ({ s, v: s.values[metric][i] }))
        .sort((a, b) => (b.v ?? -1) - (a.v ?? -1))
        .forEach(({ s, v }) => {
          const r = document.createElement("div");
          r.className = "r";
          const key = document.createElement("i");
          key.className = "key";
          key.style.background = color(s);
          const val = document.createElement("b");
          val.textContent = v == null ? "–" : m.fmt(v);
          const who = document.createElement("span");
          who.className = "who";
          who.textContent = s.id === view.me ? "You" : s.name;
          r.append(key, val, who);
          tip.append(r);
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
      show(Math.max(0, Math.min(n - 1, Math.round(((px - pad.l) / (iw || 1)) * (n - 1)))));
    });
    hit.addEventListener("pointerleave", hide);
    svg.onkeydown = (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      const i = hover == null ? n - 1 : hover + (ev.key === "ArrowRight" ? 1 : -1);
      show(Math.max(0, Math.min(n - 1, i)));
    };
    svg.onblur = hide;

    drawChartTable(series, metric, m);
  }

  function drawChartTable(series, metric, m) {
    const c = view.chart;
    const box = $("chart-table");
    box.replaceChildren();
    const table = document.createElement("table");
    const head = document.createElement("tr");
    const th0 = document.createElement("th");
    th0.textContent = c.unit === "day" ? "Day" : c.unit === "week" ? "Week of" : "Month";
    head.append(th0);
    series.forEach((s) => {
      const th = document.createElement("th");
      th.textContent = s.id === view.me ? "You" : s.name;
      head.append(th);
    });
    table.append(head);
    c.labels.forEach((lab, i) => {
      const tr = document.createElement("tr");
      const t0 = document.createElement("td");
      t0.textContent = lab;
      tr.append(t0);
      series.forEach((s) => {
        const v = s.values[metric][i];
        const cell = document.createElement("td");
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

  // ---- breakdown ---------------------------------------------------------

  function drawBreakdown() {
    const body = $("breakdown-rows");
    body.replaceChildren();
    const rows = sorted(view.people, sort.breakdown);
    const main = (k) => (sort.breakdown === k ? "main" : "");
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      if (p.id === view.me) tr.className = "you";
      tr.append(
        td(i + 1, "rank l"), td(p.id === view.me ? p.name + " (you)" : p.name, "name l"),
        td(fmt(p.reviews), main("reviews")),
        ...GROUPS.map((g) => td(fmt(p[g.key]), main(g.key))),
        td(fmt(p.added), main("added")),
      );
      body.append(tr);
    });
    markHeaders("breakdown", sort.breakdown);

    // One stacked bar per person, lengths on a shared scale.
    $("mix-caption").textContent = view.range.caption;
    const bars = $("bars");
    bars.replaceChildren();
    const max = Math.max(1, ...rows.map((p) => p.reviews));
    rows.forEach((p) => {
      const row = document.createElement("div");
      row.className = "bar-row" + (p.id === view.me ? " you" : "");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = p.id === view.me ? p.name + " (you)" : p.name;
      const track = document.createElement("div");
      track.className = "track";
      track.style.width = Math.max(0, (p.reviews / max) * 100) + "%";
      GROUPS.forEach((g) => {
        const n = p[g.key];
        if (!n) return;
        const seg = document.createElement("span");
        seg.className = "seg-bar " + g.cls;
        seg.style.flex = n + " 1 0";
        seg.tabIndex = 0;
        const pct = p.reviews ? Math.round((100 * n) / p.reviews) : 0;
        const text = `${g.label}: ${fmt(n)} (${pct}%)`;
        seg.setAttribute("aria-label", `${p.name}, ${text}`);
        const show = () => {
          const tip = $("mix-tip");
          tip.replaceChildren();
          const v = document.createElement("b");
          v.textContent = fmt(n) + " ";
          const l = document.createElement("span");
          l.className = "who";
          l.textContent = `${g.label}, ${pct}% of ${p.id === view.me ? "your" : p.name + "'s"} answers`;
          tip.append(v, l);
          tip.hidden = false;
          const box = seg.getBoundingClientRect(), host = bars.parentElement.getBoundingClientRect();
          tip.style.left = Math.min(host.width - 240, Math.max(0, box.left - host.left)) + "px";
          tip.style.top = box.bottom - host.top + 6 + "px";
        };
        seg.addEventListener("pointerenter", show);
        seg.addEventListener("focus", show);
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
    sorted(lg.rows, sort.league).forEach((r, i) => {
      const tr = document.createElement("tr");
      if (r.id === view.me) tr.className = "you";
      const medals = td("");
      ["gold", "silver", "bronze"].forEach((m) => {
        const n = (r.medals || {})[m];
        if (n) {
          const pill = document.createElement("span");
          pill.className = "medal " + m;
          pill.textContent = m[0].toUpperCase() + m.slice(1) + " " + n;
          medals.append(pill);
        }
      });
      tr.append(
        td(i + 1, "rank l"), td(r.id === view.me ? r.name + " (you)" : r.name, "name l"),
        td(fmt(r.xp), sort.league === "xp" ? "main" : ""),
        td(fmt(r.reviews)), td(fmt(Math.round(r.minutes))),
        td(r.reviews ? r.retention.toFixed(1) + "%" : "–"),
        td(Math.round(r.daysPercent) + "%"), medals,
      );
      body.append(tr);
    });
    markHeaders("league", sort.league);
  }

  // ---- shell -------------------------------------------------------------

  function render(v) {
    const first = view === null;
    view = v;
    if (first && v.pinned && v.pinnedMetric) sort.board = v.pinnedMetric;
    $("status").classList.remove("error");
    $("status").textContent = v.updated ? "Updated " + v.updated : "";
    document.querySelectorAll("#ranges button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.key === v.range.key)));
    $("custom").classList.toggle("open", v.range.key === "custom");
    $("from").value = v.range.from;
    $("to").value = v.range.to;
    $("from").max = $("to").max = v.range.today;
    $("caption").textContent = v.range.caption;
    $("announce").hidden = !v.announcement;
    $("announce").textContent = v.announcement;
    $("pin").setAttribute("aria-pressed", String(v.pinned));
    $("pin").textContent = v.pinned ? "Pinned to home screen" : "Pin to home screen";
    drawBoard();
    drawChart();
    drawBreakdown();
    drawLeague();
  }

  function loading(on) {
    if (on) $("status").textContent = view ? "Checking for new results…" : "Loading…";
  }

  function error(message) {
    $("status").textContent = message;
    $("status").classList.add("error");
  }

  function tab(name) {
    ["board", "breakdown", "league"].forEach((t) => {
      $("tab-" + t).setAttribute("aria-selected", String(t === name));
      $("panel-" + t).hidden = t !== name;
    });
    $("range-block").hidden = name === "league";
    $("rankby").hidden = name !== "board";
    if (name === "board" && view) drawChart();
  }

  function sortBy(panel, key) {
    sort[panel] = key;
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
  }

  function togglePin() {
    if (!view) return;
    pycmd((view.pinned ? "pin:off:" : "pin:on:") + sort.board);
  }

  function applyCustom() {
    pycmd("range:custom:" + $("from").value + ":" + $("to").value);
  }

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
  ["board", "breakdown", "league"].forEach((panel) => {
    document.querySelectorAll(`#panel-${panel} th[data-sort] button`).forEach((btn) => {
      btn.addEventListener("click", () => sortBy(panel, btn.parentElement.dataset.sort));
    });
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => view && drawChart(), 100);
  });

  return { render, loading, error, tab, sortBy, applyCustom, togglePin, toggleChartTable };
})();
</script>
"""
