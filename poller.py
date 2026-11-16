#!/usr/bin/env python3
"""
EXPA new-applicant watcher.

Polls an EXPA endpoint every POLL_INTERVAL_SECONDS, diffs the applicant IDs
against a locally-persisted "seen" set, and fires a Telegram notification
for every genuinely new applicant. Designed to run inside a GitHub Actions
job that restarts periodically (see run_loop() / MAX_RUNTIME_SECONDS) so a
single job never has to outlive GitHub's per-job time limit.

Everything that depends on EXPA's actual request/response shape lives in
two clearly marked functions near the top: build_request() and
extract_applicants(). Edit those once you know exactly what EXPA returns;
nothing else in the file should need to change.
"""

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration (all from environment variables / GitHub Secrets)
# ---------------------------------------------------------------------------

EXPA_URL = os.environ["EXPA_URL"]
EXPA_METHOD = os.environ.get("EXPA_METHOD", "GET").upper()
EXPA_HEADERS = json.loads(os.environ.get("EXPA_HEADERS_JSON", "{}"))
EXPA_HEADERS.pop("authorization", None)  # the access token is injected dynamically instead
EXPA_BODY_RAW = os.environ.get("EXPA_BODY_JSON", "")
EXPA_BODY = json.loads(EXPA_BODY_RAW) if EXPA_BODY_RAW.strip() else None

# OAuth2 refresh-token flow against AIESEC's auth server, so the script never
# needs a manually-recaptured, short-lived browser token again. The access
# token this gets us is only valid for a couple of hours; we proactively
# renew it well before it expires (see TokenManager below).
EXPA_TOKEN_URL = os.environ["EXPA_TOKEN_URL"]
EXPA_CLIENT_ID = os.environ["EXPA_CLIENT_ID"]
EXPA_CLIENT_SECRET = os.environ["EXPA_CLIENT_SECRET"]
EXPA_REFRESH_TOKEN = os.environ["EXPA_REFRESH_TOKEN"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
# Keep this comfortably under GitHub's 6-hour hard job limit so the job
# exits cleanly (and commits state) before it gets killed mid-request.
MAX_RUNTIME_SECONDS = float(os.environ.get("MAX_RUNTIME_SECONDS", str(5 * 3600 + 40 * 60)))

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_ids.json"))
REQUEST_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# OAuth access-token management
# ---------------------------------------------------------------------------

class TokenManager:
    """
    Keeps a valid access token around, renewing it via the refresh_token
    grant before it expires (or immediately on a 401). AIESEC's refresh
    tokens *might* rotate on use (i.e. the response hands back a new
    refresh_token that must replace the old one); we handle that in memory
    for the rest of this process, and — since there's no safe way to rewrite
    a GitHub Actions secret from inside a run without granting it extra
    permissions — we alert over Telegram if a rotation happens, so you can
    update the EXPA_REFRESH_TOKEN secret by hand just that once.
    """

    # Renew this long before the real expiry (observed to be ~7200s) so a
    # slow network blip never lets us run with an actually-expired token.
    RENEW_MARGIN_SECONDS = 15 * 60

    def __init__(self):
        self.access_token = None
        self.refresh_token = EXPA_REFRESH_TOKEN
        self.expires_at = 0  # monotonic time
        self._rotation_alert_sent = False

    def _mask(self, token):
        return f"{token[:4]}…{token[-4:]}" if token and len(token) > 8 else "∅"

    def refresh(self):
        resp = requests.post(
            EXPA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": EXPA_CLIENT_ID,
                "client_secret": EXPA_CLIENT_SECRET,
            },
            headers={"accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()

        self.access_token = data["access_token"]
        expires_in = float(data.get("expires_in", 3600))
        self.expires_at = time.monotonic() + expires_in

        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            print(f"[warn] EXPA issued a NEW refresh token on renewal "
                  f"(old {self._mask(self.refresh_token)} -> new {self._mask(new_refresh)}). "
                  "Updating in-memory copy for this run.", file=sys.stderr)
            self.refresh_token = new_refresh
            if not self._rotation_alert_sent:
                send_telegram(
                    "⚠️ EXPA rotated your refresh token on renewal.\n"
                    f"New refresh_token: {new_refresh}\n\n"
                    "Please update the EXPA_REFRESH_TOKEN secret in the repo to this value "
                    "so future restarts keep working (this running job will keep working "
                    "fine on its own until it next restarts)."
                )
                self._rotation_alert_sent = True

        print(f"[info] obtained access token (expires in {int(expires_in)}s).")

    def get_valid_access_token(self):
        if self.access_token is None or time.monotonic() > self.expires_at - self.RENEW_MARGIN_SECONDS:
            self.refresh()
        return self.access_token


token_manager = TokenManager()


# ---------------------------------------------------------------------------
# EXPA-specific logic — EDIT THESE TWO FUNCTIONS once you know the real
# request/response shape. Everything below this section is generic.
# ---------------------------------------------------------------------------

def build_request():
    """
    Return (method, url, headers, json_body) for the applicant-list request.

    Defaults to env-var-driven config captured from your browser's
    "Copy as cURL". If EXPA needs query params that change (pagination,
    timestamps, etc.) adjust here. The Authorization header is always the
    freshest access token from TokenManager, auto-renewed as needed.
    """
    headers = dict(EXPA_HEADERS)
    headers["authorization"] = token_manager.get_valid_access_token()
    return EXPA_METHOD, EXPA_URL, headers, EXPA_BODY


def extract_applicants(response_json):
    """
    Parses EXPA's ApplicationIndexQuery GraphQL response:

        {"data": {"allOpportunityApplication": {"data": [...], "paging": {...}}}}

    Each item in "data" looks roughly like:

        {
          "key": "7206858",              # this is the application id (aliased from `id`)
          "status": "open",
          "created_at": "2026-11-15T09:12:00Z",
          "opportunity": {"title": "...", "branch": {"company": {"name": "..."}}},
          "person": {"full_name": "...", "email": "..."},
          ...
        }

    Returns {application_id (str): display_text (str)}.
    """
    try:
        items = response_json["data"]["allOpportunityApplication"]["data"]
    except (KeyError, TypeError):
        print(f"[warn] unexpected response shape: {json.dumps(response_json)[:500]}", file=sys.stderr)
        return {}

    applicants = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        applicant_id = str(item.get("key") or "")
        if not applicant_id:
            continue

        person = item.get("person") or {}
        opportunity = item.get("opportunity") or {}
        company = ((opportunity.get("branch") or {}).get("company") or {}).get("name", "")

        name = person.get("full_name") or "Unknown applicant"
        title = opportunity.get("title") or ""
        status = item.get("status") or ""
        applied_at = item.get("created_at") or ""

        parts = [name]
        if title:
            parts.append(title)
        if company:
            parts.append(f"@ {company}")
        display = " — ".join(parts)
        if status:
            display += f"\nStatus: {status}"
        if applied_at:
            display += f"\nApplied: {applied_at}"

        applicants[applicant_id] = display

    return applicants


# ---------------------------------------------------------------------------
# Generic plumbing below — shouldn't need edits
# ---------------------------------------------------------------------------

def load_seen_ids():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            print(f"[warn] couldn't parse {STATE_FILE}, starting fresh", file=sys.stderr)
    return None  # None = "no state file yet" (distinct from an empty set)


def save_seen_ids(seen_ids):
    STATE_FILE.write_text(json.dumps(sorted(seen_ids), indent=0))


def persist_seen_ids(seen_ids, commit_message):
    """
    Save state to disk AND commit+push it to the repo immediately, so the
    next job (restarting every few hours) picks up where this one left off
    even if this job later crashes or gets killed. Retries on push conflicts
    since overlapping scheduled runs can race each other.
    """
    save_seen_ids(seen_ids)

    if os.environ.get("GIT_PERSIST", "1") != "1":
        return  # allows disabling git persistence for local testing

    try:
        subprocess.run(["git", "add", str(STATE_FILE)], check=True, capture_output=True, text=True)
        commit = subprocess.run(
            ["git", "commit", "-m", commit_message], capture_output=True, text=True
        )
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return
            print(f"[warn] git commit failed: {commit.stdout}{commit.stderr}", file=sys.stderr)
            return

        for attempt in range(5):
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                return
            print(f"[warn] git push attempt {attempt + 1} failed, retrying after pull --rebase: "
                  f"{push.stdout}{push.stderr}", file=sys.stderr)
            subprocess.run(
                ["git", "pull", "--rebase", "--autostash"], capture_output=True, text=True
            )
        print("[warn] gave up pushing state after retries; next job will re-derive from EXPA.",
              file=sys.stderr)
    except Exception as e:
        print(f"[warn] git persistence error: {e}", file=sys.stderr)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            print(f"[warn] telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[warn] telegram send error: {e}", file=sys.stderr)


def fetch_applicants():
    method, url, headers, body = build_request()
    resp = requests.request(
        method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
    )

    if resp.status_code == 401:
        # Our proactive renewal should normally prevent this, but if EXPA
        # invalidated the token early for any reason, force a refresh and
        # retry once immediately rather than waiting for the next loop tick.
        print("[warn] got 401, forcing an early token refresh and retrying once.", file=sys.stderr)
        token_manager.refresh()
        method, url, headers, body = build_request()
        resp = requests.request(
            method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )

    resp.raise_for_status()
    return extract_applicants(resp.json())


def run_loop():
    start_time = time.monotonic()
    seen_ids = load_seen_ids()
    first_run_ever = seen_ids is None
    if first_run_ever:
        seen_ids = set()
        print("[info] no prior state file — this run will establish a baseline "
              "without notifying for pre-existing applicants.")

    consecutive_errors = 0

    while time.monotonic() - start_time < MAX_RUNTIME_SECONDS:
        loop_start = time.monotonic()
        try:
            current = fetch_applicants()
            consecutive_errors = 0

            if first_run_ever:
                seen_ids = set(current.keys())
                persist_seen_ids(seen_ids, "chore: establish initial applicant baseline")
                first_run_ever = False
                print(f"[info] baseline established with {len(seen_ids)} existing applicant(s).")
            else:
                new_ids = set(current.keys()) - seen_ids
                if new_ids:
                    for aid in new_ids:
                        message = f"🚨 New EXPA applicant!\n{current[aid]}\n\nGo claim it on the sheet now."
                        send_telegram(message)
                        print(f"[info] notified for new applicant {aid}: {current[aid]}")
                    seen_ids |= new_ids
                    persist_seen_ids(seen_ids, f"chore: mark {len(new_ids)} applicant(s) as seen")

        except requests.RequestException as e:
            consecutive_errors += 1
            print(f"[warn] request failed ({consecutive_errors} in a row): {e}", file=sys.stderr)
            if consecutive_errors == 1 or consecutive_errors % 20 == 0:
                # Don't spam Telegram on every single failure, but do alert
                # on the first one and then periodically if it stays broken
                # (most likely cause: the captured auth token expired).
                send_telegram(
                    "⚠️ EXPA watcher is failing to fetch applicants "
                    f"(error #{consecutive_errors}): {e}\n"
                    "Access tokens now renew automatically, so this is likely something else "
                    "(e.g. the refresh token itself was revoked, or EXPA changed something) — "
                    "worth checking the Actions logs."
                )
        except Exception:
            print("[error] unexpected exception:", file=sys.stderr)
            traceback.print_exc()

        elapsed = time.monotonic() - loop_start
        sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)

    print("[info] approaching max runtime for this job, exiting cleanly so the next scheduled run can take over.")


if __name__ == "__main__":
    run_loop()
