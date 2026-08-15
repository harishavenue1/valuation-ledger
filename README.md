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
   ```
4. Deploy. The retrieve form is disabled until the secret is set.

## Notes

- Session cookies expire periodically — when retrieval starts failing, get a fresh `sessionid` and update it (locally: edit `.streamlit/secrets.toml`; deployed: update the Cloud dashboard's Secrets panel).
- `cache/` (your retrieved company data) is git-ignored — personal to each deployment, not meant to be shared via the repo.
- `screener_fetch.py` holds all the Screener.in scraping logic — no other file needed to make a fetch.
