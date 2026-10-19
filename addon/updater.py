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

REPO = "denisfliu/anki-friends-leaderboard"
PACKAGE = "friends_leaderboard"
CHECK_EVERY_S = 24 * 3600
MAX_BYTES = 5 * 1024 * 1024

_checking = False


def maybe_check(cfg: dict, version: str, save) -> None:
    if time.time() - cfg.get("last_update_check", 0) >= CHECK_EVERY_S:
        check(version, save, manual=False)


def check(version: str, save, manual: bool) -> None:
    global _checking
    if _checking:
        return
    if _is_dev_copy():
        if manual:
            showInfo("This copy of the add-on is a development copy, so it doesn't update itself.")
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
        tooltip(
            f"Friends Leaderboard updated to {latest}. Restart Anki to use the new version.",
            period=8000,
        )

    mw.taskman.run_in_background(work, done)


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
