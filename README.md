# Valuation Ledger

A minimal Streamlit app: retrieve companies live from [Screener.in](https://www.screener.in) and see them in a summary table. Self-contained — no dependency on any other project, so it runs the same locally or deployed to [Streamlit Community Cloud](https://streamlit.io/cloud).

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml, paste in your Screener.in sessionid cookie
streamlit run app.py
```

**Getting your session cookie:** log into screener.in in your browser, open DevTools → Application (Chrome) / Storage (Firefox) → Cookies → `https://www.screener.in`, copy the `sessionid` value.

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io), point it at the repo, `app.py` as the entry point.
3. In the app's **Settings → Secrets**, add:
   ```toml
   SCREENER_SESSION_ID = "your-cookie-value"
   GITHUB_TOKEN = "your-fine-grained-pat"
   ```
4. Deploy. The retrieve form is disabled until `SCREENER_SESSION_ID` is set.

## Notes

- Session cookies expire periodically — when retrieval starts failing, get a fresh `sessionid` and update it (locally: edit `.streamlit/secrets.toml`; deployed: update the Cloud dashboard's Secrets panel).
- `cache/` (your retrieved company data + scenario edits) is git-ignored — personal to each machine's disk, and does **not** survive a Streamlit Community Cloud redeploy on its own.
- **All data — retrieved company/price data (`data/all_stocks.json`) and scenario edits (`data/scenarios.json`, your Base/Bull/Bear/Mgmt inputs) — is synced to real tracked files in this repo** via the GitHub Contents API, whenever `GITHUB_TOKEN` is set in secrets (see `.streamlit/secrets.toml.example` for how to generate one). This is what makes both survive redeploys and stay consistent across devices. Without a token, both fall back to the local `cache/` files on whichever machine is running the app (unchanged from before this existed).
  - Company/price data refreshes automatically, once per calendar day, the first time *anyone* opens the app that day — no button click needed. This is tracked in `data/last_refresh.json` (also GitHub-synced, so one device's visit satisfies the day for all of them) and only re-fetches when today's date isn't marked done yet; "🔄 Refresh all now" still works for an on-demand refresh anytime. Scenario growth/PE inputs only change when you edit them. **Implied CAGR is never stored** — it's recomputed live on every page render from whatever price + EPS/PE are currently on file and the browser's current date, so it moves day-to-day purely because price does, independent of whether you've touched growth or PE for a given case that day.
  - The auto-refresh is triggered by a visit, not a true unattended cron — a day nobody opens the app is a day prices don't move. It's also blocking: the first visitor of the day waits through Screener.in fetches for every tracked ticker before the page renders (fine for a handful of tickers; would need a real scheduler like GitHub Actions if the list grows large).
- `screener_fetch.py` holds all the Screener.in scraping logic — no other file needed to make a fetch.
