# Valuation Ledger

A minimal Streamlit app: retrieve companies live from [Screener.in](https://www.screener.in) and see them in a summary table. Self-contained — no dependency on any other project, so it runs the same locally or deployed to [Streamlit Community Cloud](https://streamlit.io/cloud).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

No secrets required to fetch data — Screener's search API, top-ratios, and full multi-year P&L table all return complete data to a fully anonymous request (verified 2026-08-15 against both a large-cap and a micro-cap SME). `SCREENER_SESSION_ID` is optional and currently unused by anything this app fetches; only set it (copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml`) if a future Screener change ends up requiring it for some ticker.

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io), point it at the repo, `app.py` as the entry point.
3. In the app's **Settings → Secrets**, add:
   ```toml
   GITHUB_TOKEN = "your-fine-grained-pat"   # optional, for durable scenario data — see below
   APP_PASSWORD = "pick-your-own-password"  # strongly recommended — see below
   ```
4. Deploy.

## Notes

- **No access control by default — set `APP_PASSWORD` before you rely on this being private.** Every write in this app (Retrieve, Refresh, editing scenario inputs, Remove) runs server-side using the app's own embedded `GITHUB_TOKEN`, with no check on who's using the browser. A public GitHub repo does *not* by itself let random people write to it (GitHub separates read/write permissions) — but a public, ungated *app URL* functionally does, since the app acts as a privileged proxy for whoever's driving it. Setting `APP_PASSWORD` puts a password screen in front of everything; leave it unset only if you're fine with anyone who has the URL being able to add/remove companies and overwrite your scenario data.
- No Screener.in login/cookie is needed for anything this app does — retrieval and refresh work fully anonymously. If retrieval ever does start failing with an auth-looking error, that would mean Screener changed something; check `screener_fetch.py`'s parsing logic before assuming a cookie is the fix.
- `cache/` (your retrieved company data + scenario edits) is git-ignored — personal to each machine's disk, and does **not** survive a Streamlit Community Cloud redeploy on its own.
- **All data — retrieved company/price data (`data/all_stocks.json`) and scenario edits (`data/scenarios.json`, your Base/Bull/Bear/Mgmt inputs) — is synced to real tracked files in this repo** via the GitHub Contents API, whenever `GITHUB_TOKEN` is set in secrets (see `.streamlit/secrets.toml.example` for how to generate one). This is what makes both survive redeploys and stay consistent across devices. Without a token, both fall back to the local `cache/` files on whichever machine is running the app (unchanged from before this existed).
  - Company/price data refreshes automatically, once per calendar day, the first time *anyone* opens the app that day — no button click needed. This is tracked in `data/last_refresh.json` (also GitHub-synced, so one device's visit satisfies the day for all of them) and only re-fetches when today's date isn't marked done yet; "🔄 Refresh all now" still works for an on-demand refresh anytime. Scenario growth/PE inputs only change when you edit them. **Implied CAGR is never stored** — it's recomputed live on every page render from whatever price + EPS/PE are currently on file and the browser's current date, so it moves day-to-day purely because price does, independent of whether you've touched growth or PE for a given case that day.
  - The auto-refresh is triggered by a visit, not a true unattended cron — a day nobody opens the app is a day prices don't move. It's also blocking: the first visitor of the day waits through Screener.in fetches for every tracked ticker before the page renders (fine for a handful of tickers; would need a real scheduler like GitHub Actions if the list grows large).
- `screener_fetch.py` holds all the Screener.in scraping logic — no other file needed to make a fetch.
