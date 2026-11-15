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
EXPA_BODY_RAW = os.environ.get("EXPA_BODY_JSON", "")
EXPA_BODY = json.loads(EXPA_BODY_RAW) if EXPA_BODY_RAW.strip() else None

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
# Keep this comfortably under GitHub's 6-hour hard job limit so the job
# exits cleanly (and commits state) before it gets killed mid-request.
MAX_RUNTIME_SECONDS = float(os.environ.get("MAX_RUNTIME_SECONDS", str(5 * 3600 + 40 * 60)))

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_ids.json"))
REQUEST_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# EXPA-specific logic — EDIT THESE TWO FUNCTIONS once you know the real
# request/response shape. Everything below this section is generic.
# ---------------------------------------------------------------------------

def build_request():
    """
    Return (method, url, headers, json_body) for the applicant-list request.

    Defaults to env-var-driven config captured from your browser's
    "Copy as cURL". If EXPA needs query params that change (pagination,
    timestamps, etc.) adjust here.
    """
    return EXPA_METHOD, EXPA_URL, EXPA_HEADERS, EXPA_BODY


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
                    "The captured auth/session may have expired — it might need refreshing."
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
