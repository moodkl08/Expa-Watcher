# EXPA new-applicant watcher

Polls EXPA for new applicants to your opportunities and pings you on Telegram
the moment a new one shows up, so you can be first on the sheet. Runs for
free, 24/7, on GitHub Actions — no laptop, no server, no cost.

## How it works

- `poller.py` hits the EXPA endpoint every 5 seconds, compares the applicant
  IDs it sees against `seen_ids.json` (committed to this repo), and sends a
  Telegram message for any ID it hasn't seen before.
- A single run loops for ~5h45m then exits cleanly (GitHub kills jobs after
  6h, so this leaves a safety margin).
- `.github/workflows/watch.yml` schedules a fresh run every 3 hours, so runs
  overlap and there's no real gap in coverage even if GitHub's scheduler is
  running late (common during peak hours — it's a "best effort" scheduler).
- Every time a new applicant is detected, the updated `seen_ids.json` is
  committed and pushed immediately (with retry-on-conflict), so state
  survives job restarts and crashes.

## ⚠️ Repo must be public

GitHub Actions minutes are **unlimited and free only on public repositories**.
On a private repo you'd burn through the free 2,000 min/month allowance in
about two days running this continuously. `seen_ids.json` only ever stores
anonymous applicant ID numbers (never names), specifically so this is safe
to keep in a public repo. Don't change the parser to store names/emails in
that file.

## One-time setup

1. **Create the repo.** On GitHub, create a new **public** repository (e.g.
   `expa-watcher`). Don't initialize it with a README (we already have one).

2. **Push this code.** From this folder:
   ```bash
   git remote add origin https://github.com/<your-username>/expa-watcher.git
   git branch -M main
   git add -A
   git commit -m "Initial EXPA watcher setup"
   git push -u origin main
   ```
   (If you'd rather not use the command line, you can instead drag this
   whole folder onto GitHub's "uploading an existing file" page for a new
   repo — it preserves the folder structure including `.github/workflows`.)

3. **Add secrets.** In the repo: Settings → Secrets and variables → Actions →
   "New repository secret". Add each of these (exact values given to you
   separately in chat, not duplicated in this file since they contain your
   live EXPA auth token):

   | Secret name | Value |
   |---|---|
   | `EXPA_URL` | `https://gis-api.aiesec.org/graphql` |
   | `EXPA_METHOD` | `POST` |
   | `EXPA_HEADERS_JSON` | the headers JSON block from chat |
   | `EXPA_BODY_JSON` | the GraphQL body JSON block from chat |
   | `TELEGRAM_BOT_TOKEN` | your bot token from BotFather |
   | `TELEGRAM_CHAT_ID` | your Telegram chat ID |

   This is already wired to your real committee/programme filters (EXPA's
   own `ApplicationIndexQuery`), fetching the newest 30 applications each
   poll — the parser in `poller.py` is already filled in to match.

4. **Test it.** Go to the Actions tab → "EXPA applicant watcher" → "Run
   workflow" → tick "test_mode" → Run. This does a quick ~60-second run so
   you can confirm it fetches EXPA successfully and (on the very first ever
   run) sends no false notification, just establishes the baseline. Check
   the job logs for errors.

5. **Go live.** Once the test run succeeds, you don't need to do anything
   else — the schedule in `watch.yml` takes over and it runs continuously,
   laptop closed or not.

## Known limitation: auth expiry

The `authorization` header in `EXPA_HEADERS_JSON` is a snapshot of your
browser session at the moment you captured it. It's an opaque token (not a
JWT), which often means it's longer-lived than a typical short expiry
access token — but we don't know its actual lifetime yet. If the poller
starts failing with 401/403 errors, you'll get a Telegram warning about it;
at that point just recapture a fresh token the same way (DevTools → Network
→ the `graphql` request → copy the `authorization` header value) and update
the `EXPA_HEADERS_JSON` secret. If this turns out to expire often enough to
be annoying, it's worth checking whether EXPA has a longer-lived API key
option for team leaders — ask me if so.
