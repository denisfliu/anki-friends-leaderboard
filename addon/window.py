"""The leaderboard window: Leaderboard and League tabs in an Anki web view.

Python fetches the board and does the math (ranking.py); the page only lays
out what it's given, switches tabs and sorts columns. It asks Python for a new
date range through pycmd, and Python downloads older days only when a range
needs them.
"""

import datetime
import json
import os

from aqt import mw
from aqt.qt import QDialog, QVBoxLayout
from aqt.webview import AnkiWebView

from . import client, ranking, stats

DAY = datetime.timedelta(days=1)

# Last board download, shared across windows and kept on disk so the window
# opens instantly with the last standings while it checks for new ones. The
# server answers "unchanged" when nothing new was uploaded since.
_cache = None
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
    _dialog = LeaderboardDialog(cfg)
    _dialog.show()


def forget_board() -> None:
    global _cache
    _cache = None
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass


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


class LeaderboardDialog(QDialog):
    def __init__(self, cfg: dict):
        super().__init__(mw)
        self.cfg = cfg
        self.board = None
        self.range = ("today", None, None)
        self.setWindowTitle("Friends Leaderboard")
        self.web = AnkiWebView(parent=self, title="friends_leaderboard")
        self.web.set_bridge_command(self.on_command, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.resize(720, 640)
        self.web.stdHtml(PAGE, context=self)
        _load_cache()
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
            key = parts[1]
            a = parts[2] if len(parts) > 2 else None
            b = parts[3] if len(parts) > 3 else None
            self.range = (key, a, b)
            self.load()

    # -- data

    def load(self, check: bool = False) -> None:
        """Show the current range. `check` asks the server whether anything
        changed; otherwise cached data is used when it covers the range."""
        if mw.col is None:
            return
        today = stats.anki_today(mw.col)
        start, _, _ = range_dates(*self.range, today=today)
        settings = (_cache or {}).get("board", {}).get("settings") or {}
        need = ranking.oldest_needed(today, settings, start)
        covered = _cache and _cache["since"] <= need.isoformat()
        if covered:
            self.board = _cache["board"]
            self.render(today)
            if not check:
                return
        self.fetch(need, today)

    def fetch(self, need: datetime.date, today: datetime.date) -> None:
        since = need.isoformat()
        payload = {"action": "board", "token": self.cfg["token"], "since": since}
        if _cache and _cache["since"] <= since:
            payload["since"] = _cache["since"]
            payload["ifChangedSince"] = _cache["lastWrite"]
        url = self.cfg["server_url"]
        self.web.eval("lb.loading(true)")

        def on_done(fut):
            global _cache
            if _dialog is not self:
                return
            try:
                board = fut.result()
            except Exception as e:
                self.web.eval(f"lb.error({json.dumps(str(e))})")
                return
            if board.get("unchanged") and _cache:
                board = _cache["board"]
            else:
                _cache = {
                    "since": payload["since"],
                    "lastWrite": board.get("lastWrite", ""),
                    "board": board,
                    "fetched": datetime.datetime.now(),
                }
            _cache["fetched"] = datetime.datetime.now()
            _save_cache()
            self.board = board
            # The season start lives in group settings; fetch again if it
            # turns out to need older days than this download covered.
            start, _, _ = range_dates(*self.range, today=today)
            if ranking.oldest_needed(today, board.get("settings"), start).isoformat() < _cache["since"]:
                self.fetch(ranking.oldest_needed(today, board.get("settings"), start), today)
                return
            self.render(today)

        mw.taskman.run_in_background(lambda: client.call(url, payload), on_done)

    def render(self, today: datetime.date) -> None:
        board = self.board or {}
        start, end, caption = range_dates(*self.range, today=today)
        st = ranking.standings(board, today, start, end)
        lg = ranking.league(board, today)
        medals = lg.get("medals", {})
        for row in lg.get("rows", []):
            row["medals"] = medals.get(row["id"], {})
        fetched = (_cache or {}).get("fetched")
        view = {
            "me": st["me"],
            "announcement": str(st["settings"].get("announcement") or ""),
            "updated": fetched.strftime("%I:%M %p").lstrip("0") if fetched else "",
            "range": {
                "key": self.range[0],
                "caption": caption,
                "from": (start or today).isoformat(),
                "to": (end or today).isoformat(),
                "today": today.isoformat(),
            },
            "people": st["people"],
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
    --muted: #5f6368; --line: #e2e2e6; --accent: #1f5fc9; --you: #e8f0fe;
    --gold-bg: #fbefc8; --gold-fg: #6b4e00; --silver-bg: #e9ecf0; --silver-fg: #454b55;
    --bronze-bg: #f6e1d0; --bronze-fg: #7a4318;
    margin: 0; background: var(--bg); color: var(--text);
    font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
  }
  body.night-mode, body.nightMode, body.night_mode, html.night-mode body {
    --bg: #232326; --surface: #2d2d31; --track: #1b1b1d; --text: #ececef;
    --muted: #a8a8b0; --line: #3a3a40; --accent: #7aa7ff; --you: #26344d;
    --gold-bg: #4a3b14; --gold-fg: #f3d27a; --silver-bg: #3b3e44; --silver-fg: #d5d9e0;
    --bronze-bg: #45301f; --bronze-fg: #e9b48a;
  }
  button, select, input { font: inherit; color: var(--text); }
  .wrap { padding: 0 22px 18px; display: flex; flex-direction: column; gap: 14px; min-height: 100vh; box-sizing: border-box; }
  .tabs { display: flex; align-items: flex-end; gap: 4px; border-bottom: 1px solid var(--line); }
  .tab { height: 44px; padding: 0 14px; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; color: var(--muted); cursor: pointer; }
  .tab[aria-selected="true"] { border-bottom-color: var(--accent); color: var(--text); font-weight: 650; }
  .spacer { flex: 1; }
  .status { align-self: center; color: var(--muted); font-size: 12px; margin-right: 8px; }
  .btn { height: 28px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); cursor: pointer; }
  .tabs .btn { align-self: center; }
  .seg { display: flex; padding: 3px; gap: 2px; border-radius: 8px; background: var(--track); }
  .seg button { flex: 1 1 0; height: 30px; border: 0; border-radius: 6px; background: none; color: var(--muted); cursor: pointer; }
  .seg button[aria-pressed="true"] { background: var(--bg); color: var(--text); font-weight: 600; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .caption { flex: 1; color: var(--muted); }
  label { color: var(--muted); }
  select, input[type=date] { height: 30px; padding: 0 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); }
  .custom { display: none; }
  .custom.open { display: flex; }
  table { width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; font-variant-numeric: tabular-nums; }
  th { background: var(--surface); color: var(--muted); font-size: 12px; font-weight: 600; padding: 9px 10px; text-align: right; white-space: nowrap; }
  th button { all: unset; cursor: pointer; }
  th.sorted { color: var(--text); }
  td { padding: 10px; border-top: 1px solid var(--line); text-align: right; white-space: nowrap; }
  th.l, td.l { text-align: left; }
  td.rank { color: var(--muted); width: 22px; }
  td.name { width: 99%; overflow: hidden; text-overflow: ellipsis; max-width: 0; }
  td.main { font-weight: 600; }
  tr.you td { background: var(--you); }
  tr.you td.name { font-weight: 650; }
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
</style>
<div class="wrap">
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="tab-board" aria-selected="true" onclick="lb.tab('board')">Leaderboard</button>
    <button class="tab" role="tab" id="tab-league" aria-selected="false" onclick="lb.tab('league')">League</button>
    <span class="spacer"></span>
    <span class="status" id="status">Loading…</span>
    <button class="btn" onclick="pycmd('refresh')">Refresh</button>
  </div>

  <section id="panel-board">
    <div style="display:flex;flex-direction:column;gap:14px">
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
        <label for="metric">Rank by</label>
        <select id="metric" onchange="lb.sortBy('board', this.value)">
          <option value="reviews">Reviews</option>
          <option value="minutes">Minutes</option>
          <option value="streak">Streak</option>
          <option value="past31">Past 31 days</option>
          <option value="retention">Retention</option>
        </select>
      </div>
      <table>
        <thead><tr>
          <th class="l">#</th><th class="l">Name</th>
          <th data-sort="reviews"><button>Reviews</button></th>
          <th data-sort="minutes"><button>Minutes</button></th>
          <th data-sort="streak"><button>Streak</button></th>
          <th data-sort="past31"><button>Past 31 days</button></th>
          <th data-sort="retention"><button>Retention</button></th>
        </tr></thead>
        <tbody id="board-rows"></tbody>
      </table>
    </div>
  </section>

  <section id="panel-league" hidden>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="season"><h2 id="season-title"></h2><span class="caption" id="season-dates"></span><b id="season-left"></b></div>
      <table>
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
  const sort = { board: "reviews", league: "xp" };
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n || 0).toLocaleString();

  function td(text, cls) {
    const el = document.createElement("td");
    if (cls) el.className = cls;
    el.textContent = text;
    return el;
  }

  function sorted(rows, key) {
    return rows.slice().sort((a, b) => (b[key] - a[key]) || a.name.localeCompare(b.name));
  }

  function markHeaders(panel, key) {
    document.querySelectorAll(`#panel-${panel} th[data-sort]`).forEach((th) => {
      const on = th.dataset.sort === key;
      th.classList.toggle("sorted", on);
      th.querySelector("button").textContent = th.querySelector("button").textContent.replace(/ ▾$/, "") + (on ? " ▾" : "");
    });
  }

  function drawBoard() {
    const body = $("board-rows");
    body.replaceChildren();
    const rows = sorted(view.people, sort.board);
    if (!rows.length) {
      const tr = document.createElement("tr");
      const cell = td("Nobody has joined yet.", "empty");
      cell.colSpan = 7;
      tr.append(cell);
      body.append(tr);
    }
    rows.forEach((p, i) => {
      const tr = document.createElement("tr");
      if (p.id === view.me) tr.className = "you";
      tr.append(
        td(i + 1, "rank l"), td(p.id === view.me ? p.name + " (you)" : p.name, "name l"),
        td(fmt(p.reviews), sort.board === "reviews" ? "main" : ""),
        td(fmt(Math.round(p.minutes)), sort.board === "minutes" ? "main" : ""),
        td(fmt(p.streak), sort.board === "streak" ? "main" : ""),
        td(fmt(p.past31), sort.board === "past31" ? "main" : ""),
        td(p.reviews ? p.retention.toFixed(1) + "%" : "–", sort.board === "retention" ? "main" : ""),
      );
      body.append(tr);
    });
    markHeaders("board", sort.board);
    $("metric").value = sort.board;
  }

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

  function render(v) {
    view = v;
    loading(false);
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
    drawBoard();
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
    ["board", "league"].forEach((t) => {
      $("tab-" + t).setAttribute("aria-selected", String(t === name));
      $("panel-" + t).hidden = t !== name;
    });
  }

  function sortBy(panel, key) {
    sort[panel] = key;
    if (view) panel === "board" ? drawBoard() : drawLeague();
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
  ["board", "league"].forEach((panel) => {
    document.querySelectorAll(`#panel-${panel} th[data-sort] button`).forEach((btn) => {
      btn.addEventListener("click", () => sortBy(panel, btn.parentElement.dataset.sort));
    });
  });

  return { render, loading, error, tab, sortBy, applyCustom };
})();
</script>
"""
