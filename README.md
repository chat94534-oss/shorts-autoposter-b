# the channel — autopost

Fully automated YouTube Shorts channel: 2nd-person "Would you survive X?" scenarios with a
burned-in ticking death timer. GitHub Actions builds and posts one Short per scheduled slot:
Claude-authored topic bank (`topics.json`) → edge-tts narration → 8 Pollinations images →
ffmpeg (Ken Burns, cross-dissolves, atmosphere + dread-rumble bed, timer, grade, captions) →
YouTube upload. The workflow commits the consumed bank/state back to the repo after each post.

- **Schedule (EDT):** 12 PM, 3 PM, 6 PM, 9 PM — see `.github/workflows/post.yml`.
  Crons are UTC: **in November (DST ends) bump each cron hour +1** or posts shift an hour early.
- **Secrets (repo settings):** `CLIENT_SECRET_JSON`, `TOKEN_JSON` (published OAuth app —
  refresh token does not expire).
- **Manual run:** Actions → post → Run workflow (choose privacy for tests).
- **Top up the bank:** append episodes to `topics.json` (same schema) and push.
