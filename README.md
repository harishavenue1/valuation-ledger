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
- **Scenario edits (Base/Bull/Bear/Mgmt inputs) are synced to `data/scenarios.json`, a real tracked file in this repo**, via the GitHub Contents API, whenever `GITHUB_TOKEN` is set in secrets (see `.streamlit/secrets.toml.example` for how to generate one). This is what makes edits survive redeploys and stay consistent across devices — without it, edits only live in the local `cache/` file on whichever machine is running the app. Retrieved-company data (`cache/all_stocks.json`) is *not* synced this way since it's one click to re-fetch via "🔄 Refresh all now".
- `screener_fetch.py` holds all the Screener.in scraping logic — no other file needed to make a fetch.
