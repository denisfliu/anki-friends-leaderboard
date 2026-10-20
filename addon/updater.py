"""Installs new versions from the project's GitHub releases.

At most once a day, the add-on asks GitHub for the latest release. If it's
newer, the .ankiaddon file attached to it is downloaded, checked and installed
with Anki's own installer, which keeps everyone's settings. The new version
runs after Anki restarts. This never touches the leaderboard server.
"""

import io
import json
import os
import tempfile
import time
import zipfile

import requests
from aqt import mw
from aqt.utils import showInfo, tooltip

from . import client

REPO = "denisfliu/anki-friends-leaderboard"
PACKAGE = "friends_leaderboard"
CHECK_EVERY_S = 24 * 3600
MAX_BYTES = 5 * 1024 * 1024

_checking = False
_waiting = []  # called with no arguments when a running check finishes

# The running version (set by __init__) and the newest release seen, so the
# window can show the update icon. The daily startup check installs updates
# on its own; Refresh asks GitHub sooner, at most once a minute (GitHub
# allows 60 unauthenticated requests an hour per computer).
CURRENT = "0"
LATEST_GAP_S = 60
_latest = {"version": "", "checked": 0.0}


def update_state(cfg: dict) -> dict:
    """{"state": "none" | "available" | "restart", "version": ...} for the icon."""
    pending = cfg.get("pending_version") or ""
    if pending and _newer(pending, CURRENT):
        return {"state": "restart", "version": pending}
    latest = _latest["version"] or cfg.get("latest_seen") or ""
    if latest and _newer(latest, CURRENT):
        return {"state": "available", "version": latest}
    return {"state": "none", "version": ""}


def forget_finished_update(cfg: dict, save) -> None:
    """After a restart onto the pending version, stop showing the restart icon."""
    pending = cfg.get("pending_version")
    if pending and not _newer(pending, CURRENT):
        cfg["pending_version"] = ""
        save(cfg)


def refresh_latest(save, on_change) -> None:
    """On Refresh: ask GitHub for the newest release, at most once a minute."""
    if _is_dev_copy() or time.time() - _latest["checked"] < LATEST_GAP_S:
        return
    _latest["checked"] = time.time()

    def work():
        rel = requests.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=20,
        )
        rel.raise_for_status()
        return str(rel.json().get("tag_name", "")).lstrip("v")

    def done(fut):
        try:
            latest = fut.result()
        except Exception:
            return
        if not latest:
            return
        _latest["version"] = latest
        cfg = mw.addonManager.getConfig(PACKAGE)
        if cfg.get("latest_seen") != latest:
            cfg["latest_seen"] = latest
            save(cfg)
        on_change()

    client.in_background(work, done)


def maybe_check(cfg: dict, version: str, save) -> None:
    if time.time() - cfg.get("last_update_check", 0) >= CHECK_EVERY_S:
        check(version, save, manual=False)


def check(version: str, save, manual: bool, on_finish=None) -> None:
    global _checking
    if on_finish:
        _waiting.append(on_finish)
    if _checking:
        return
    if _is_dev_copy():
        if manual:
            showInfo("This copy of the add-on is a development copy, so it doesn't update itself.")
        _finish()
        return
    _checking = True

    def work():
        rel = requests.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
        rel.raise_for_status()
        rel = rel.json()
        latest = str(rel.get("tag_name", "")).lstrip("v")
        if not _newer(latest, version):
            return latest, None
        asset = next(
            (a for a in rel.get("assets", []) if a.get("name", "").endswith(".ankiaddon")),
            None,
        )
        if not asset:
            return latest, None
        resp = requests.get(asset["browser_download_url"], timeout=60)
        resp.raise_for_status()
        data = resp.content
        if len(data) > MAX_BYTES:
            raise ValueError("The update file is too large.")
        _verify(data)
        fd, path = tempfile.mkstemp(suffix=".ankiaddon")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return latest, path

    def done(fut):
        try:
            _done(fut)
        finally:
            _finish()

    def _done(fut):
        global _checking
        _checking = False
        cfg = mw.addonManager.getConfig(PACKAGE)
        cfg["last_update_check"] = int(time.time())
        save(cfg)
        try:
            latest, path = fut.result()
        except Exception as e:
            if manual:
                showInfo(f"Anki couldn't check for leaderboard updates.\n\n{e}")
            return
        _latest["version"] = latest or _latest["version"]
        if not path:
            if manual:
                tooltip(f"Friends Leaderboard {version} is the newest version.")
            return
        try:
            result = mw.addonManager.install(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if type(result).__name__ == "InstallError":
            if manual:
                showInfo(f"The update to {latest} didn't install. ({getattr(result, 'errmsg', '')})")
            return
        cfg = mw.addonManager.getConfig(PACKAGE)
        cfg["pending_version"] = latest
        save(cfg)
        tooltip(
            f"Friends Leaderboard updated to {latest}. Restart Anki to use the new version.",
            period=8000,
        )

    client.in_background(work, done)


def _finish() -> None:
    global _checking
    _checking = False
    waiting = list(_waiting)
    _waiting.clear()
    for fn in waiting:
        try:
            fn()
        except Exception:
            pass


def _verify(data: bytes) -> None:
    """Refuse anything that isn't this add-on."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        if "manifest.json" not in names or "__init__.py" not in names:
            raise ValueError("The update file isn't a Friends Leaderboard add-on.")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("package") != PACKAGE:
            raise ValueError("The update file is for a different add-on.")
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise ValueError("The update file has unsafe paths.")


def _newer(a: str, b: str) -> bool:
    def parts(v):
        out = []
        for p in v.split("."):
            digits = "".join(ch for ch in p if ch.isdigit())
            out.append(int(digits or 0))
        return out

    try:
        return parts(a) > parts(b)
    except Exception:
        return False


def _is_dev_copy() -> bool:
    folder = os.path.dirname(os.path.abspath(__file__))
    return os.path.islink(folder) or os.path.exists(os.path.join(folder, ".git"))
