"""Talks to the Apps Script web app. Safe to call from a background thread."""

import json
import time

import requests

# Apps Script usually answers in 1 to 3 seconds, but a cold start or a slow
# network can take much longer, so wait generously and retry what's safe.
TIMEOUT = 60
# Actions that can be sent twice without harm (uploads overwrite the same rows).
SAFE_TO_RETRY = {"ping", "info", "board", "upload", "feed"}
RETRIES = 2

ERRORS = {
    "bad_invite": "That invite code doesn't match.",
    "bad_name": "Names must be 1 to 24 characters.",
    "name_taken": "Someone on the leaderboard already has that name. If it's you, ask the admin to reset your sign-in, then join again.",
    "bad_token": "The server doesn't recognize this computer. Please join again.",
    "group_full": "The leaderboard is full.",
    "busy": "The server is busy. Please try again in a moment.",
    "slow_down": "That was too soon after the last request. Please try again in a moment.",
    "forbidden": "Only a leaderboard admin can do that.",
    "not_found": "That person isn't on the leaderboard anymore.",
    "last_admin": "The leaderboard needs at least one admin.",
    "timeout": "The server took too long to answer. Please try again.",
    "offline": "Anki couldn't reach the server. Please check your internet connection.",
}


class ServerError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        message = ERRORS.get(code, code)
        super().__init__(f"{message} ({detail})" if detail else message)


def call(url: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    retries = RETRIES if payload.get("action") in SAFE_TO_RETRY else 0
    for attempt in range(retries + 1):
        try:
            return _call_once(url, payload, timeout)
        except ServerError as e:
            if e.code != "busy" or attempt == retries:
                raise
        except requests.Timeout:
            if attempt == retries:
                raise ServerError("timeout")
        except requests.ConnectionError:
            if attempt == retries:
                raise ServerError("offline")
        time.sleep(3 * (attempt + 1))


def _call_once(url: str, payload: dict, timeout: float) -> dict:
    # Apps Script answers a POST with a redirect to the result, which requests
    # follows as a GET. It reads the raw body, so the content type is cosmetic.
    resp = requests.post(
        url,
        data=json.dumps(payload),
        headers={"Content-Type": "text/plain;charset=utf-8"},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError:
        raise ServerError(
            "The server didn't send back leaderboard data. Check that server_url"
            " is the web app URL and that the deployment allows access by Anyone."
        )
    if not body.get("ok"):
        raise ServerError(body.get("error", "unknown error"), body.get("detail", ""))
    return body
