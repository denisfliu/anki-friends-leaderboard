"""Friends Leaderboard: a private Anki leaderboard backed by a Google Sheet.

Network use is kept to a minimum. Stats upload only after a sync (or when
someone joins or chooses Upload my stats now), and only if something changed.
The board downloads when someone opens the leaderboard window, and the home
screen checks for changes at most every 10 minutes.
"""

import time

from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu, qconnect
from aqt.utils import askUser, chooseList, getText, showInfo, showText, showWarning, tooltip

from . import client, home, stats, updater, window

VERSION = "0.7.1"
RETRY_MS = 10_000
CHUNK_GAP_S = 6
# Bump to make everyone re-upload their full daily history once.
HISTORY_VERSION = 2  # 2: learning/young/mature/relearning and added cards
ADDON = __name__

_busy = False
_pending = False
# Anki runs the close hook first, then syncs, then quits. The upload after
# that closing sync has to finish before Anki quits, so it waits.
_closing = False


def _config() -> dict:
    return mw.addonManager.getConfig(ADDON)


def _save(cfg: dict) -> None:
    mw.addonManager.writeConfig(ADDON, cfg)


def _ready(cfg: dict, manual: bool) -> bool:
    if not cfg.get("server_url"):
        if manual:
            showWarning("Add the leaderboard's server_url in Tools > Add-ons > Config first.")
        return False
    if not cfg.get("token"):
        if manual:
            showInfo("Join the leaderboard first: Tools > Friends Leaderboard > Join.")
        return False
    return mw.col is not None


def upload(manual: bool = False, blocking: bool = False) -> None:
    global _busy, _pending
    cfg = _config()
    if not _ready(cfg, manual):
        return
    latest = stats.change_marker(mw.col)
    # Full daily history goes up once (again whenever HISTORY_VERSION changes,
    # for example when a new daily stat is added). After that, only recent days.
    full = not blocking and cfg.get("history_version", 0) < HISTORY_VERSION
    if not manual and not full and latest == cfg.get("last_uploaded_marker"):
        return
    if _busy and not blocking:
        _pending = True
        return

    data = stats.collect(mw.col, None if full else stats.HISTORY_DAYS)
    days = data["days"]
    chunks = [days[i : i + stats.CHUNK_DAYS] for i in range(0, len(days), stats.CHUNK_DAYS)]
    base = {"action": "upload", "token": cfg["token"], "version": VERSION, "totals": data["totals"]}
    payloads = [dict(base, days=chunk) for chunk in chunks] or [dict(base, days=[])]
    url = cfg["server_url"]

    def send(timeout=None, retries=None):
        for i, payload in enumerate(payloads):
            if i:
                time.sleep(CHUNK_GAP_S)  # the server takes one upload per player every few seconds
            client.call(url, payload, timeout=timeout, retries=retries)

    def mark_uploaded():
        cfg = _config()
        cfg["last_uploaded_marker"] = latest
        if full:
            cfg["history_version"] = HISTORY_VERSION
        _save(cfg)

    if blocking:
        # Anki is closing; one quick try so it never hangs for long.
        try:
            send(timeout=8, retries=0)
            mark_uploaded()
        except Exception:
            pass
        return

    def on_done(fut):
        global _busy, _pending
        _busy = False
        retry_soon = False
        try:
            fut.result()
        except client.ServerError as e:
            if manual:
                showWarning(f"Your stats didn't upload.\n\n{e}")
            retry_soon = e.code == "slow_down"
        except Exception as e:
            if manual:
                showWarning(f"Your stats didn't upload.\n\n{e}")
        else:
            mark_uploaded()
            home.mark_stale()
            if manual:
                tooltip("Your stats are uploaded.")
        if retry_soon:
            # The server allows one upload per player every few seconds.
            _pending = False
            mw.progress.timer(RETRY_MS, upload, False)
        elif _pending:
            _pending = False
            upload()

    _busy = True
    client.in_background(send, on_done)


def join() -> None:
    cfg = _config()
    if not cfg.get("server_url"):
        showWarning("Add the leaderboard's server_url in Tools > Add-ons > Config first.")
        return
    if cfg.get("token") and not askUser(
        f"This computer has already joined as {cfg.get('name')}. Join again with a new name?"
    ):
        return
    name, ok = getText("Your name on the leaderboard:", title="Join the leaderboard")
    if not ok or not name.strip():
        return
    invite, ok = getText("Invite code:", title="Join the leaderboard")
    if not ok or not invite.strip():
        return

    def on_done(fut):
        try:
            body = fut.result()
        except client.ServerError as e:
            if e.code == "timeout":
                showWarning(
                    "The server took too long to answer, so you might have joined anyway.\n\n"
                    "Please try joining again. If it says the name is taken, ask the admin to "
                    "reset your sign-in (Tools > Friends Leaderboard > Admin), then join again."
                )
            else:
                showWarning(f"You couldn't join the leaderboard.\n\n{e}")
            return
        except Exception as e:
            showWarning(f"You couldn't join the leaderboard.\n\n{e}")
            return
        player = body["player"]
        cfg = _config()
        cfg["name"] = player["name"]
        cfg["player_id"] = player["id"]
        cfg["admin"] = bool(player.get("admin"))
        cfg["token"] = body["token"]
        cfg["last_uploaded_marker"] = ""
        cfg["history_version"] = 0
        _save(cfg)
        window.forget_board()
        _rebuild_menu()
        tooltip(f"You joined as {player['name']}. Your stats will upload in the background.", period=5000)
        upload()

    tooltip("Joining the leaderboard…")
    payload = {"action": "join", "name": name.strip(), "invite": invite.strip()}
    client.in_background(
        lambda: client.call(cfg["server_url"], payload), on_done
    )


def show_board() -> None:
    cfg = _config()
    if _ready(cfg, manual=True):
        window.open_leaderboard(cfg)


# ---------------------------------------------------------------------------
# Admin. The server only lets admins do these; the menu shows for admins.


def _admin_call(payload: dict, then) -> None:
    cfg = _config()
    if not _ready(cfg, manual=True):
        return
    payload = dict(payload, token=cfg["token"])

    def on_done(fut):
        try:
            body = fut.result()
        except Exception as e:
            showWarning(str(e))
            return
        then(body)

    client.in_background(lambda: client.call(cfg["server_url"], payload), on_done)


def _pick_player(prompt: str, then) -> None:
    def got(board):
        me = (board.get("me") or {}).get("id")
        people = [p for p in board.get("players", []) if p["id"] != me]
        if not people:
            showInfo("Nobody else is on the leaderboard yet.")
            return
        labels = [p["name"] + ("" if p.get("active") else " (removed)") for p in people]
        i = chooseList(prompt, labels)
        if i is not None and 0 <= i < len(people):
            then(people[i])

    _admin_call({"action": "board", "includeInactive": True, "since": "9999-01-01"}, got)


def admin_invite_code() -> None:
    _admin_call(
        {"action": "info"},
        lambda body: showText(
            "The invite code is:\n\n" + str(body.get("settings", {}).get("inviteCode", "")),
            copyBtn=True,
            title="Invite code",
        ),
    )


def admin_reset_signin() -> None:
    def reset(person):
        if askUser(
            f"Reset {person['name']}'s sign-in? They'll keep their stats and can join again "
            "with the same name and the invite code."
        ):
            _admin_call(
                {"action": "admin", "op": "player", "id": person["id"], "resetToken": True, "active": True},
                lambda _: tooltip(f"{person['name']} can join again now."),
            )

    _pick_player("Whose sign-in should be reset?", reset)


def admin_remove() -> None:
    def remove(person):
        if askUser(
            f"Remove {person['name']} from the leaderboard? Their stats are kept, and they "
            "can come back by joining again with the same name."
        ):
            _admin_call(
                {"action": "admin", "op": "player", "id": person["id"], "active": False},
                lambda _: (window.forget_board(), tooltip(f"{person['name']} was removed.")),
            )

    _pick_player("Who should be removed?", remove)


def admin_announcement() -> None:
    def got(body):
        current = str(body.get("settings", {}).get("announcement") or "")
        text, ok = getText(
            "Announcement shown at the bottom of everyone's leaderboard (leave empty to clear it):",
            default=current,
            title="Announcement",
        )
        if not ok:
            return
        change = {"set": {"announcement": text.strip()}} if text.strip() else {"unset": ["announcement"]}
        _admin_call(
            dict({"action": "admin", "op": "settings"}, **change),
            lambda _: tooltip("The announcement is saved."),
        )

    _admin_call({"action": "info"}, got)


def check_updates() -> None:
    updater.check(VERSION, _save, manual=True)


# ---------------------------------------------------------------------------
# Menu and hooks

_menu = None


def _rebuild_menu() -> None:
    global _menu
    if _menu is not None:
        mw.form.menuTools.removeAction(_menu.menuAction())
    _menu = QMenu("Friends Leaderboard", mw)
    mw.form.menuTools.addMenu(_menu)
    items = [
        ("Show leaderboard", show_board),
        ("Upload my stats now", lambda: upload(manual=True)),
        ("Join…", join),
        ("Check for updates", check_updates),
    ]
    for label, action in items:
        _add(_menu, label, action)
    pin = QAction("Show on home screen", mw)
    pin.setCheckable(True)
    pin.setChecked(bool(_config().get("home_pin")))
    qconnect(pin.toggled, _set_home_pin)
    _menu.addAction(pin)

    def sync_pin():
        # The window's pin button changes the same setting.
        pin.blockSignals(True)
        pin.setChecked(bool(_config().get("home_pin")))
        pin.blockSignals(False)

    qconnect(_menu.aboutToShow, sync_pin)
    if _config().get("admin"):
        admin = _menu.addMenu("Admin")
        for label, action in (
            ("Show invite code", admin_invite_code),
            ("Post announcement…", admin_announcement),
            ("Reset someone's sign-in…", admin_reset_signin),
            ("Remove someone…", admin_remove),
        ):
            _add(admin, label, action)
    version = QAction(f"Version {VERSION}", mw)
    version.setEnabled(False)
    _menu.addSeparator()
    _menu.addAction(version)


def _set_home_pin(on: bool) -> None:
    cfg = _config()
    cfg["home_pin"] = bool(on)
    cfg.setdefault("home_range", ["today", None, None])
    cfg.setdefault("home_metric", "reviews")
    _save(cfg)
    if mw.state == "deckBrowser":
        mw.deckBrowser.refresh()


def _add(menu, label, action) -> None:
    item = QAction(label, mw)
    qconnect(item.triggered, lambda _checked=False, action=action: action())
    menu.addAction(item)


def _on_sync_finished() -> None:
    upload(blocking=_closing)


def _on_profile_close() -> None:
    global _closing
    _closing = True


def _on_profile_open() -> None:
    global _closing
    _closing = False
    _learn_admin()
    updater.maybe_check(_config(), VERSION, _save)


def _learn_admin() -> None:
    # Older installs didn't save whether this person is an admin; ask once.
    cfg = _config()
    if "admin" in cfg or not cfg.get("token") or not cfg.get("server_url"):
        return

    def on_done(fut):
        try:
            me = fut.result().get("me") or {}
        except Exception:
            return
        cfg = _config()
        cfg["admin"] = bool(me.get("admin"))
        _save(cfg)
        _rebuild_menu()

    payload = {"action": "info", "token": cfg["token"]}
    client.in_background(lambda: client.call(cfg["server_url"], payload), on_done)


_rebuild_menu()
home.setup(show_board)
gui_hooks.profile_did_open.append(_on_profile_open)
gui_hooks.sync_did_finish.append(_on_sync_finished)
gui_hooks.profile_will_close.append(_on_profile_close)
