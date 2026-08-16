#!/usr/bin/env python3
"""
Valuation Ledger — self-contained Streamlit app.

Runs identically local or deployed (Streamlit Community Cloud via
GitHub): no absolute local paths, no import from any other project's
scripts (see screener_fetch.py, bundled alongside this file).

No secrets required to fetch data (verified 2026-08-15) — Screener's
search API, top-ratios, and full P&L table all work fully anonymously;
SCREENER_SESSION_ID is optional and currently unused by any fetch this
app makes (see get_session_id() / fetch_one()'s docstring). GITHUB_TOKEN
is the one secret worth setting once deployed — see the "GitHub-backed
sync" section below for why. Both come from st.secrets — Streamlit's
own mechanism, which transparently reads .streamlit/secrets.toml
locally and the dashboard-configured secrets when deployed, so the same
code path works in both places with zero branching.

Setup (local): none required to run. Optionally copy
.streamlit/secrets.toml.example to .streamlit/secrets.toml and fill in
GITHUB_TOKEN if you want scenario edits synced (git-ignored, never
commit the real value).

Setup (Streamlit Community Cloud): push this repo to GitHub, deploy at
share.streamlit.io, then optionally add GITHUB_TOKEN under the app's
Settings -> Secrets in the same TOML format.

Run locally:
    streamlit run app.py
"""
import hmac, json, os, re, time
from datetime import date, datetime

import streamlit as st

from screener_fetch import fetch_one, fetch_price_only

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
GUIDANCE_DIR = os.path.join(CACHE_DIR, "guidance")
SCENARIOS_PATH = os.path.join(CACHE_DIR, "scenarios.json")
ALL_STOCKS_PATH = os.path.join(CACHE_DIR, "all_stocks.json")
LAST_REFRESH_PATH = os.path.join(CACHE_DIR, "last_refresh.json")
GUIDANCE_TRACKER_PATH = os.path.join(CACHE_DIR, "guidance_tracker.json")
GUIDANCE_TAGS = ["", "Beat", "Neutral", "Miss"]
GUIDANCE_TAG_CLASS = {"Beat": "vl-tag-beat", "Neutral": "vl-tag-neutral", "Miss": "vl-tag-miss"}

FY_LABEL_RE = re.compile(r"^(Mar|Jun|Sep|Dec)\s+\d{4}$")
ARRAY_FIELDS = ["revenue", "revenue_growth_pct", "expenses", "operating_profit", "opm_pct",
                "other_income", "interest", "depreciation", "pbt", "tax_pct", "net_profit",
                "pat_growth_pct", "eps", "shares_cr"]

CASES = ["base", "bull", "bear", "mgmt"]
CASE_LABEL = {"base": "Base Case", "bull": "Bull Case", "bear": "Bear Case", "mgmt": "Management Case"}
CASE_COLOR = {"base": "#B7C0BB", "bull": "#63C46E", "bear": "#E3776A", "mgmt": "#C4A8E8"}
N_EST_YEARS = 3

# Fallback Revenue Growth % seed when a company has no guidance research
# yet (default_case_state()) — a rough, admittedly generic starting
# point (2026-08-16 request: "later user can read the concall, write up
# commentary and edit back the growth rates to match up") so every case
# computes a CAGR out of the box instead of sitting on "fill PE"/blank
# until someone types something in. Never applied to Management Case,
# same as guidance seeding.
DEFAULT_REV_GROWTH = {"base": 20.0, "bull": 25.0, "bear": 15.0}

# Stale-data flag (2026-08-16 request) — "fundamentals_fetched_at" only
# moves forward on a full fetch_one() run (see screener_fetch.py), not
# the daily price-only refresh, so this measures actual EMA/quarterly/
# P&L staleness rather than being fooled by a fresh price. 7 days
# roughly matches a weekly "Refresh all now" cadence; a company that
# hasn't had one in longer than that gets flagged.
STALE_DAYS = 7

# Management Case is dropped from the Future Projections grid and the
# Summary page table (explicit request, 2026-08-15) — it's never
# guidance-seeded anyway and stayed blank/unused in both most of the
# time. It's still fully available via each company's Detail-page chip
# row and Key Assumptions. Scenario data already saved for "mgmt" is
# left untouched — nothing here deletes it, this constant just controls
# which cases get columns/chips in these two views.
GRID_CASES = [c for c in CASES if c != "mgmt"]

st.set_page_config(page_title="Valuation Ledger", page_icon="🧮", layout="wide")


# ───────────────────────── Access gate (via st.secrets) ─────────────────────────

def check_password():
    """True if the app is unlocked for this browser session — either an
    APP_PASSWORD secret isn't configured (open access, same posture as
    before this existed — matches the optional-secret pattern used
    elsewhere in this file), or the visitor already entered it correctly
    this session, or they just did on this run.

    Why this exists (added 2026-08-16): every write in this app — Retrieve,
    Refresh, editing scenario inputs, Remove — runs server-side using the
    app's own embedded GITHUB_TOKEN. There's no per-visitor distinction
    otherwise: anyone who reaches the deployed URL can drive all of it,
    including deleting a tracked company, with zero trace of who did it.
    A public GitHub repo doesn't grant randoms write access on its own
    (see README) — but a public *app URL* with no gate functionally does,
    since the app acts as a privileged proxy for whoever's using it."""
    try:
        app_password = st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title("🧮 Valuation Ledger")
    # Capped to roughly the title's own rendered width (~405px, measured;
    # 460px leaves a little breathing room) — 2026-08-16, "make it wide
    # only for width of the valuation ledger header": reverses the two
    # earlier passes, which stretched the field toward full page width.
    # This keeps the whole login block visually aligned under the title
    # instead of sprawling edge-to-edge. Injected only on this branch
    # (never reached once authenticated), so it can't affect the main
    # app's layout on a later rerun.
    st.markdown('<style>.st-key-vl_pwd_form_wrap { max-width: 460px; }</style>', unsafe_allow_html=True)

    with st.container(key="vl_pwd_form_wrap"):
        # st.form (not a bare text_input + button) so pressing Enter inside
        # the password field submits it — Streamlit forms submit on Enter
        # from any of their input widgets natively, no JS needed
        # (2026-08-16 request).
        with st.form("_pwd_form"):
            pwd = st.text_input("Password", type="password", key="_pwd_input")
            submitted = st.form_submit_button("Unlock")
    if submitted:
        # Constant-time compare — trivial to add, avoids a timing side
        # channel on password length/prefix for what little it's worth.
        if hmac.compare_digest(pwd, app_password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


# ───────────────────────── Session cookie (via st.secrets) ─────────────────────────

def get_session_id():
    """Optional (verified 2026-08-15) — Screener's search API, top-ratios,
    and full P&L table all work fully anonymously; see fetch_one()'s
    docstring in screener_fetch.py. Returns None if not configured,
    which is the normal case now; callers no longer gate on this, they
    just pass it through to fetch_one() where it's a no-op when falsy."""
    try:
        return st.secrets["SCREENER_SESSION_ID"]
    except (KeyError, FileNotFoundError):
        return None


# ───────────────────────── Data I/O ─────────────────────────

def clean_stock(raw):
    """Strips a trailing "TTM" (or other non-"Mon YYYY") column Screener
    sometimes appends when quarterly data has outpaced the last fiscal
    year-end — left in, it breaks anything reading the latest actual
    year off years[-1]. Same fix as the earlier ValuationTool skill
    build (2026-08-13), ported here since this repo doesn't import that
    code."""
    years = raw["years"]
    fy_idx = [i for i, y in enumerate(years) if FY_LABEL_RE.match(y)]
    stock = {k: v for k, v in raw.items() if k not in ARRAY_FIELDS and k != "years"}
    stock["years"] = [years[i] for i in fy_idx]
    for f in ARRAY_FIELDS:
        stock[f] = [raw[f][i] for i in fy_idx]
    return stock


def _local_load_raw_all_stocks():
    if not os.path.exists(ALL_STOCKS_PATH):
        return {}
    with open(ALL_STOCKS_PATH) as f:
        return json.load(f)


def _local_save_raw_all_stocks(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ALL_STOCKS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_raw_all_stocks():
    """Unclean (TTM column, if any, intact) — only save_all_stocks()
    should write this shape back out, so a re-clean on next load stays
    idempotent regardless of how many times a stock gets refreshed.
    GitHub-synced the same way as scenarios (see _github_synced_load) —
    company/price data is "all the data" too, not just scenario edits,
    so it needs to survive a redeploy and match across devices the same
    way."""
    cfg = get_github_config()
    path = cfg["stocks_path"] if cfg else "data/all_stocks.json"
    return _github_synced_load("all_stocks_raw_cache", "all_stocks_sha", path,
                                _local_load_raw_all_stocks, _local_save_raw_all_stocks, "company data")


def save_all_stocks(data):
    cfg = get_github_config()
    path = cfg["stocks_path"] if cfg else "data/all_stocks.json"
    _github_synced_save(data, "all_stocks_raw_cache", "all_stocks_sha", path,
                         _local_save_raw_all_stocks, "company data")


def merge_fetched(existing, new_data):
    """A fresh fetch_one() result is a complete replacement dict with no
    knowledge of local-only fields this app adds on top of Screener's own
    data — currently just "owned" (2026-08-16, "divide summary into
    stocks I own / tracking"). Every call site that does raw[t] = data
    after a fetch needs to go through this instead, or a Refresh silently
    wipes the owned flag back to unset. `existing` is the ticker's prior
    raw[t] dict if any (None for a brand-new company, which has nothing
    to preserve)."""
    if existing:
        new_data = {**new_data, "owned": existing.get("owned", False)}
    return new_data


def merge_price_only(existing, price_data):
    """For the lightweight price-only refresh (2026-08-16, "2 refresh for
    fundamental data & prices alone, as prices do need daily updates") —
    unlike merge_fetched() (a full fetch_one() replacement, preserving
    only "owned"), this OVERLAYS price_data's handful of fields (ticker/
    name/current_price/pe_ratio/market_cap_cr/week52_high/fetched_at)
    onto the existing record, preserving everything else as-is
    (years/revenue/.../ema*/quarters/owned — fetch_price_only() never
    touches any of that, so there's nothing there to lose)."""
    if not existing:
        return price_data
    return {**existing, **price_data}


def load_all_stocks():
    raw = load_raw_all_stocks()
    return {ticker: clean_stock(v) for ticker, v in raw.items()}


def _local_load_last_refresh():
    if not os.path.exists(LAST_REFRESH_PATH):
        return {}
    with open(LAST_REFRESH_PATH) as f:
        return json.load(f)


def _local_save_last_refresh(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_REFRESH_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_last_refresh():
    """{"date": "YYYY-MM-DD"} of the last successful auto-refresh — kept
    in GitHub too (not just local) so a visit from any device marks the
    day done for every device, not just the one that happened to open
    first."""
    return _github_synced_load("last_refresh_cache", "last_refresh_sha", "data/last_refresh.json",
                                _local_load_last_refresh, _local_save_last_refresh, "refresh timestamp")


def save_last_refresh(data):
    _github_synced_save(data, "last_refresh_cache", "last_refresh_sha", "data/last_refresh.json",
                         _local_save_last_refresh, "refresh timestamp")


def load_guidance(ticker):
    p = os.path.join(GUIDANCE_DIR, f"{ticker}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _local_load_scenarios():
    if not os.path.exists(SCENARIOS_PATH):
        return {}
    with open(SCENARIOS_PATH) as f:
        return json.load(f)


def _local_save_scenarios(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SCENARIOS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ── GitHub-backed sync (so scenario edits survive a cloud redeploy and
# are the same regardless of which computer opens the app) ──
#
# cache/ is git-ignored (personal, regenerable-ish local cache) so it
# does NOT survive a Streamlit Community Cloud redeploy on its own. To
# make scenario edits durable and consistent across devices, they're
# also mirrored to a real tracked file (GITHUB_DATA_PATH, default
# "data/scenarios.json") in this repo via the GitHub Contents API,
# authenticated with a Personal Access Token in st.secrets. The local
# file stays as a fast read + offline-safe fallback: every save writes
# both; every load prefers GitHub when configured, and falls back to
# local (with a one-time warning) if the API call fails for any reason
# (no token configured, bad token, network hiccup, rate limit).
#
# Cached in st.session_state for the lifetime of the browser session so
# a Streamlit rerun (fires on every widget interaction) doesn't refetch
# from the API each time — only load_scenarios()'s first call per
# session hits the network; every save updates the cache + the stored
# sha (GitHub requires the current blob sha to update a file).

import base64
import requests


def get_github_config():
    """None if GITHUB_TOKEN isn't set — callers fall back to local-only."""
    try:
        token = st.secrets["GITHUB_TOKEN"]
    except (KeyError, FileNotFoundError):
        return None
    repo = st.secrets.get("GITHUB_REPO", "harishavenue1/valuation-ledger")
    return {
        "token": token,
        "repo": repo,
        "scenarios_path": st.secrets.get("GITHUB_DATA_PATH", "data/scenarios.json"),
        "stocks_path": st.secrets.get("GITHUB_STOCKS_PATH", "data/all_stocks.json"),
        "guidance_tracker_path": st.secrets.get("GITHUB_GUIDANCE_TRACKER_PATH", "data/guidance_tracker.json"),
    }


def _github_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def github_fetch_json(cfg, path):
    """Returns (data, sha). sha is None both when the file doesn't exist
    yet (first-ever save will create it) and when the fetch failed
    outright — callers distinguish those via the raised/caught error
    happening at the call site, not here."""
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
    resp = requests.get(url, headers=_github_headers(cfg["token"]), timeout=10)
    if resp.status_code == 404:
        return {}, None
    resp.raise_for_status()
    body = resp.json()
    content = base64.b64decode(body["content"]).decode("utf-8")
    return json.loads(content), body["sha"]


def github_put_json(cfg, path, data, sha, message):
    """Returns the new sha. Raises on failure — caller decides how to
    surface that (local save already succeeded by the time this runs,
    so a failure here never loses data, just skips the cross-device
    sync for this edit)."""
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=_github_headers(cfg["token"]), json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


def _github_synced_load(cache_key, sha_key, github_path, local_loader, local_saver, label):
    """Shared load pattern for any GitHub-synced JSON store: session-cached,
    GitHub-preferred, local-file fallback with a one-time warning."""
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    cfg = get_github_config()
    if cfg:
        try:
            data, sha = github_fetch_json(cfg, github_path)
            st.session_state[sha_key] = sha
            st.session_state[cache_key] = data
            local_saver(data)  # keep local mirror fresh as offline fallback
            return data
        except Exception as e:
            st.warning(f"⚠️ Couldn't reach GitHub for {label} sync ({e}) — using local cache. "
                       "Edits will still save locally but won't sync until the next successful load.")

    data = local_loader()
    st.session_state[cache_key] = data
    st.session_state[sha_key] = None
    return data


def _github_synced_save(data, cache_key, sha_key, github_path, local_saver, label, max_attempts=4):
    """Shared save pattern: local write always succeeds first (never lose
    data), then best-effort push to GitHub — retried with a freshly
    fetched sha up to max_attempts times, not just once.

    Why more than one retry: Streamlit fires one script rerun per widget
    commit, and typing through several driver fields in a row (growth%,
    OPM%, tax%, PE, ...) can genuinely produce reruns close enough
    together that more than one save is in flight against the same sha
    — observed live (2026-08-16): a single retry still hit a second 409.
    Each loop iteration re-fetches the current sha immediately before
    retrying, so it keeps re-aiming at whatever the latest remote state
    actually is rather than replaying the same stale guess.

    Not data-lossy on this device even if every attempt fails: every
    save writes the FULL current dict (see set_case_state()), so the
    very next successful save — for this field or any other — carries
    this one's change forward too. The real residual risk is narrower:
    a redeploy, or another device reading GitHub, in the gap before
    that next successful save."""
    local_saver(data)
    st.session_state[cache_key] = data

    cfg = get_github_config()
    if not cfg:
        return
    message = f"Update {os.path.basename(github_path)} via Valuation Ledger app"
    sha = st.session_state.get(sha_key)
    last_err = None
    for attempt in range(max_attempts):
        try:
            new_sha = github_put_json(cfg, github_path, data, sha, message)
            st.session_state[sha_key] = new_sha
            return
        except Exception as e:
            last_err = e
            try:
                _, sha = github_fetch_json(cfg, github_path)
            except Exception:
                break  # can't even read now (network down) — no point retrying further

    st.warning(f"⚠️ Saved locally, but GitHub sync of {label} failed after {max_attempts} attempts "
               f"({last_err}). This device's copy is safe; the next successful save will carry this "
               "change forward too — but until then, a redeploy or another device reading GitHub may miss it.")


def load_scenarios():
    cfg = get_github_config()
    path = cfg["scenarios_path"] if cfg else "data/scenarios.json"
    return _github_synced_load("scenarios_cache", "scenarios_sha", path,
                                _local_load_scenarios, _local_save_scenarios, "scenarios")


def save_scenarios(data):
    cfg = get_github_config()
    path = cfg["scenarios_path"] if cfg else "data/scenarios.json"
    _github_synced_save(data, "scenarios_cache", "scenarios_sha", path, _local_save_scenarios, "scenarios")


def _local_load_guidance_tracker():
    if not os.path.exists(GUIDANCE_TRACKER_PATH):
        return {"quarters": [], "tracked": [], "cells": {}}
    with open(GUIDANCE_TRACKER_PATH) as f:
        return json.load(f)


def _local_save_guidance_tracker(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(GUIDANCE_TRACKER_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_guidance_tracker():
    """{"quarters": [labels...], "tracked": [tickers...], "cells": {ticker:
    {quarter_label: {"note": str, "tag": ""|"Beat"|"Neutral"|"Miss"}}}}.
    "tracked" is this page's own row list, still removable independently
    here (its own 🗑️ only drops tracked-here membership, not the company
    itself) — but no longer independently ADDED to: a Retrieve on the
    Summary page now appends here too (2026-08-16, "if a company is
    added to summary screen, same should be available on management
    guidance tracker page also"), on top of this page's own Add a
    company form doing the same in the other direction. One-directional
    only — a company removed from here stays on the Summary page."""
    cfg = get_github_config()
    path = cfg["guidance_tracker_path"] if cfg else "data/guidance_tracker.json"
    return _github_synced_load("guidance_tracker_cache", "guidance_tracker_sha", path,
                                _local_load_guidance_tracker, _local_save_guidance_tracker, "guidance tracker")


def save_guidance_tracker(data):
    cfg = get_github_config()
    path = cfg["guidance_tracker_path"] if cfg else "data/guidance_tracker.json"
    _github_synced_save(data, "guidance_tracker_cache", "guidance_tracker_sha", path,
                         _local_save_guidance_tracker, "guidance tracker")


def last_actual(arr):
    """Walk backward for the first non-null value — defensive against a
    company whose latest FY had a one-off null (e.g. a loss year with no
    meaningful tax %)."""
    for v in reversed(arr or []):
        if v is not None:
            return v
    return None


def default_case_state(stock, ticker, case):
    """Revenue Growth % seeds from cache/guidance/<TICKER>.json when
    present (Base=range midpoint, Bull=upper, Bear=lower — never for
    Management Case, which stays open for the user's own "if management
    is right" modelling). Falls back to DEFAULT_REV_GROWTH (20/25/15)
    when there's no guidance yet (2026-08-16 request) — a placeholder the
    user is expected to overwrite once they've actually read the concall/
    management commentary, not a real estimate; guidance, when present,
    always wins over this generic default. OPM%/Tax% always carry forward
    the last actual value regardless of guidance — computeModel() needs
    both non-null before it can produce PBT/PAT/EPS at all."""
    guidance = load_guidance(ticker)
    # from_guidance (truly guidance-sourced) tracked separately from
    # guided_growth (what actually seeds the drivers below, generic
    # fallback included) — the assumptions note further down must only
    # claim "auto-filled from management guidance research" when it's
    # from_guidance, never when it's just DEFAULT_REV_GROWTH standing in
    # for a company with no guidance research done yet.
    from_guidance = guidance.get("revenue_growth", {}).get(case) if guidance and case != "mgmt" else None
    guided_growth = from_guidance
    if guided_growth is None and case != "mgmt":
        guided_growth = DEFAULT_REV_GROWTH.get(case)
    drivers = []
    for _ in range(N_EST_YEARS):
        drivers.append({
            "revGrowth": guided_growth,
            "opm": last_actual(stock.get("opm_pct")),
            "tax": last_actual(stock.get("tax_pct")),
            "other_income": last_actual(stock.get("other_income")),
            "interest": last_actual(stock.get("interest")),
            "depreciation": last_actual(stock.get("depreciation")),
            "shares": last_actual(stock.get("shares_cr")),
            # Defaults to the same value as Revenue Growth % (2026-08-16
            # request: "set PE = revenue growth ... let it editable") — a
            # PEG-of-1 starting point (25% growth → PE 25x) instead of
            # blank, so the projections grid/Summary page's Base/Bull/Bear
            # cells compute a CAGR immediately, rather than sitting on
            # "fill PE" until the user manually types one in. Still a
            # plain editable number_input either way — this only changes
            # the seed value used before anyone (or any saved scenario)
            # has touched it. Only stays None for the Management Case,
            # which never seeds a growth default at all (see above).
            "pe": guided_growth,
        })
    assumptions = ""
    if from_guidance is not None and guidance and guidance.get("source_text"):
        which = ("guidance range midpoint" if case == "base"
                 else "guidance range upper end" if case == "bull" else "guidance range lower end")
        assumptions = (
            f"[Auto-filled from management guidance research]\n\n"
            f"Revenue Growth % ({CASE_LABEL[case]}): {from_guidance}% — {which}.\n\n"
            f"Guidance: {guidance['source_text']}\n\n"
            f"Confidence: {guidance.get('confidence', '')}\n\n"
            f"Sources: {', '.join(guidance.get('source_urls', []))}\n\n"
            f"As of: {guidance.get('as_of', '')}"
        )
    return {"drivers": drivers, "assumptions": assumptions}


def get_case_state(scenarios, stock, ticker, case):
    saved = scenarios.get(ticker, {}).get(case)
    if saved and "drivers" in saved:
        return saved
    return default_case_state(stock, ticker, case)


def set_case_state(scenarios, ticker, case, state):
    scenarios.setdefault(ticker, {})[case] = state
    save_scenarios(scenarios)


# ───────────────────────── Compute engine ─────────────────────────
# Mirrors the Artifact's computeModel()/headlineCagr() exactly — same
# formulas, same sequencing. Keep in sync if either changes.

def compute_model(stock, state):
    revenue_prev = stock["revenue"][-1]
    pat_prev = stock["net_profit"][-1]
    rows = []
    for dr in state["drivers"]:
        revenue = (revenue_prev * (1 + dr["revGrowth"] / 100)
                   if dr.get("revGrowth") is not None and revenue_prev is not None else None)
        op = revenue * dr["opm"] / 100 if revenue is not None and dr.get("opm") is not None else None
        expenses = revenue - op if revenue is not None and op is not None else None
        oi = dr.get("other_income") or 0
        interest = dr.get("interest") or 0
        dep = dr.get("depreciation") or 0
        pbt = (op + oi - interest - dep) if op is not None else None
        pat = pbt * (1 - dr["tax"] / 100) if pbt is not None and dr.get("tax") is not None else None
        pat_growth = ((pat - pat_prev) / abs(pat_prev) * 100
                      if pat is not None and pat_prev not in (None, 0) else None)
        shares = dr.get("shares") or stock["shares_cr"][-1]
        eps = pat / shares if pat is not None and shares else None
        fwd_pe = (stock["current_price"] / eps
                  if eps not in (None, 0) and stock.get("current_price") else None)
        rows.append(dict(revenue=revenue, operating_profit=op, expenses=expenses, other_income=oi,
                          interest=interest, depreciation=dep, pbt=pbt, pat=pat, pat_growth=pat_growth,
                          shares=shares, eps=eps, forward_pe=fwd_pe))
        revenue_prev = revenue if revenue is not None else revenue_prev
        pat_prev = pat if pat is not None else pat_prev
    return rows


def days_until(year):
    return (date(year, 3, 31) - date.today()).days


def cagr_for(current_price, share_price, days):
    if not current_price or not share_price or share_price <= 0 or days is None or days <= 0:
        return None
    return (pow(share_price / current_price, 365 / days) - 1) * 100


def headline_cagr(stock, state):
    """The chip/summary-column CAGR — nearest estimate year that has a PE
    Multiple filled in (walk forward, not backward: a user modelling only
    FY2027 expects the Base Case chip to show FY2027, not silently skip
    ahead to FY2029 just because it happens to be checked first)."""
    model = compute_model(stock, state)
    last_year = int(stock["years"][-1].split(" ")[1])
    for i in range(N_EST_YEARS):
        dr = state["drivers"][i]
        eps = model[i]["eps"]
        if dr.get("pe") and eps is not None:
            year = last_year + i + 1
            share_price = eps * dr["pe"]
            cagr = cagr_for(stock["current_price"], share_price, days_until(year))
            # growth is never None here: eps only computes (above) when
            # revGrowth was set, since revenue — and everything downstream
            # of it, including eps — depends on it in compute_model().
            return dict(cagr=cagr, share_price=share_price, year=year,
                        growth=dr.get("revGrowth"), pe=dr["pe"])
    return None


# ───────────────────────── Format helpers ─────────────────────────

def as_float(x):
    """st.number_input requires value/step/min/max to all share one numeric
    type — mixing an int (e.g. a guidance JSON's plain `35`) with a float
    step (e.g. 0.5) raises StreamlitMixedNumericTypesError."""
    return float(x) if x is not None else None


def fmt(v, digits=0, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def fmt_signed(v, digits=1, suffix="%"):
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:,.{digits}f}{suffix}"


def est_year_label(stock, i, fy=False):
    last_year = int(stock["years"][-1].split(" ")[1])
    return f"FY{last_year + i + 1}" if fy else f"Mar {last_year + i + 1}"


# Indian fiscal year (Apr-Mar) quarter-end month → (quarter number, FY-year
# offset). Only Mar stays in the same calendar year as its own FY label;
# Jun/Sep/Dec quarter-ends fall in the FY that finishes the FOLLOWING
# March, hence +1 (e.g. "Sep 2026" is Q2 of FY27 = Apr2026-Mar2027, not
# FY26). 2-digit FY (Q2FY27, not Q2FY2027) — 2026-08-16 request: "can we
# give which quarterSalesGrowth like Q2FY27" — a different, more compact
# convention than est_year_label()'s 4-digit "FY2027" used for annual
# estimates; quarters are commonly written the short way.
FISCAL_QUARTER_MAP = {"Mar": (4, 0), "Jun": (1, 1), "Sep": (2, 1), "Dec": (3, 1)}


def fiscal_quarter_label(period_label):
    """"Sep 2026" -> "Q2FY27". Returns the input unchanged if it isn't a
    recognizable "Mon YYYY" quarter-end label (e.g. a Half-Yearly period
    for an SME-listed company reports different months than the standard
    Mar/Jun/Sep/Dec quarter-ends)."""
    if not period_label:
        return period_label
    parts = period_label.strip().split()
    if len(parts) != 2 or parts[0] not in FISCAL_QUARTER_MAP or not parts[1].isdigit():
        return period_label
    q, offset = FISCAL_QUARTER_MAP[parts[0]]
    fy = (int(parts[1]) + offset) % 100
    return f"Q{q}FY{fy:02d}"


# ───────────────────────── Theme ─────────────────────────

def inject_css():
    st.markdown("""
    <style>
      :root {
        --vl-bg: #0F1512; --vl-surface: #171F19; --vl-surface-2: #2B2312;
        --vl-ink: #E8EEE9; --vl-muted: #9CAA9F; --vl-faint: #718074;
        --vl-border: #2B362F; --vl-accent: #52C29D; --vl-brass: #E0B34D;
        --vl-good: #63C46E; --vl-bad: #E3776A;
      }
      .stApp { font-variant-numeric: tabular-nums; }
      h1, h2, h3 { letter-spacing: 0.1px; }

      table.vl-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
      table.vl-table th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
                           color: var(--vl-faint); padding: 8px 10px; border-bottom: 1px solid var(--vl-border); }
      table.vl-table td { padding: 8px 10px; border-bottom: 1px solid var(--vl-border); color: var(--vl-ink); }
      table.vl-table tr:hover td { background: rgba(82,194,157,0.06); }
      table.vl-table td.vl-num { text-align: right; font-variant-numeric: tabular-nums; }
      .vl-sub { color: var(--vl-muted) !important; font-size: 11.5px; }
      .vl-pos { color: var(--vl-good) !important; font-weight: 600; }
      .vl-neg { color: var(--vl-bad) !important; font-weight: 600; }

      .vl-empty { background: var(--vl-surface); border: 1px dashed var(--vl-border); border-radius: 12px;
                  padding: 40px 24px; text-align: center; color: var(--vl-muted); margin: 18px 0; }

      /* Detail page's slim sticky header (2026-08-16, "static header on
       * scrolling down... just like screener") — pinned to the top of
       * the viewport so the company/ticker/price stays visible however
       * far down the annual/quarterly tables or projections grid you've
       * scrolled. position:fixed, not sticky — measured (DOM walk +
       * scroll test) that Streamlit's actual scroll container is
       * section[data-testid="stMain"], a flex container, and sticky
       * simply didn't engage there (computed style showed
       * position:sticky/top:0 correctly applied, but the element still
       * scrolled off-screen with the content instead of pinning) for
       * reasons that didn't trace to any single ancestor's overflow/
       * transform; fixed relative to the viewport sidesteps needing to
       * know why. top:60px clears Streamlit's own toolbar (measured
       * height); left/right hardcode the ~80px page padding measured at
       * this app's normal wide-layout width rather than reading it live.
       * Opaque background (not transparent) is the whole point of a
       * pinned bar — otherwise page content scrolls up underneath and
       * shows through it. z-index above normal content, below the
       * toolbar itself. */
      .vl-sticky-header {
        position: fixed; top: 60px; left: 0; right: 0; z-index: 100; background: var(--vl-bg);
        /* Symmetric padding + justify-content:center (2026-08-16, "move
         * name to mid of screen as back to summary is very close to
         * name") — the earlier asymmetric 240px-left/80px-right padding
         * (to clear the Back button) skewed centering off-true by that
         * same 160px difference, landing the name right next to the
         * button instead of in the middle of the screen. Back button
         * stays independent (position:fixed, doesn't consume flex space
         * here) at left:80px, now with real distance from a name that's
         * centered across the FULL bar width instead. */
        padding: 10px 80px; border-bottom: 1px solid var(--vl-border);
        color: var(--vl-ink); display: flex; justify-content: center; align-items: baseline;
        gap: 16px; flex-wrap: wrap;
      }
      /* Company name — first bumped to match st.title()'s H1 (44px) per an
       * earlier request, then dialed back down (2026-08-16, "reduce font
       * size as its too big for static header") to match st.subheader()'s
       * own H3 size instead (measured live: 28px/600) — prominent enough
       * to read as a header, not overwhelming a bar that's pinned on
       * screen the whole time. Ticker/price/PE kept as a smaller
       * secondary line so the name stays the clear visual anchor. */
      .vl-sticky-name { font-size: 28px; font-weight: 600; line-height: 1.3; }
      .vl-sticky-header .vl-sub { font-size: 15px; font-weight: 400; }
      .vl-sticky-detail { font-size: 14px; color: var(--vl-muted); }
      .vl-sticky-sep { color: var(--vl-faint); margin: 0 10px; }

      /* Back button — fixed-positioned over the sticky bar's left side
       * (independent of the bar's own flex/centering, since fixed
       * elements don't consume flex space) instead of rendering as a
       * normal below-the-bar element, so it reads as part of the header
       * the whole time it's pinned, not a separate control underneath
       * it. top offset vertically centers it in the bar's ~54px height
       * (10px padding × 2 + ~34px line height at 28px font). */
      .st-key-vl_sticky_back_btn { position: fixed; top: 68px; left: 80px; z-index: 101; }

      .vl-stat-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0 14px; }
      .vl-stat { background: var(--vl-surface); border: 1px solid var(--vl-border); border-radius: 10px;
                 padding: 10px 16px; min-width: 110px; }
      .vl-stat-label { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.08em;
                        color: var(--vl-faint) !important; margin-bottom: 3px; }
      .vl-stat-value { font-size: 19px; font-weight: 700; color: var(--vl-ink) !important; }

      .vl-chip-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px;
                     margin: 6px 0 18px; }
      .vl-chip { background: var(--vl-surface); border: 1px solid var(--vl-border);
                 border-left: 4px solid var(--chip-color, var(--vl-accent)); border-radius: 10px;
                 padding: 12px 16px; }
      .vl-chip-label { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.07em;
                        color: var(--vl-faint) !important; margin-bottom: 3px; }
      .vl-chip-cagr { font-size: 22px; font-weight: 700; color: var(--chip-color, var(--vl-accent)) !important; }
      .vl-chip-sub { display: block; font-size: 12px; color: var(--vl-muted) !important; margin-top: 2px; }

      .vl-badge { display: inline-block; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
                  font-weight: 700; padding: 3px 9px; border-radius: 20px; margin-left: 8px; vertical-align: middle; }
      .vl-badge-guidance { color: var(--vl-brass) !important; background: rgba(224,179,77,0.14); }

      /* Management Guidance Tracker page — small Beat/Neutral/Miss pill
       * rendered under each quarter's tag selectbox (same .vl-badge
       * shape as vl-badge-guidance above, not a widget-wrapper hack —
       * simpler and matches how CAGR +/- is already color-coded via
       * plain markdown spans elsewhere, e.g. the projections grid's own
       * CAGR row). */
      .vl-tag-beat { color: var(--vl-good) !important; background: rgba(99,196,110,0.14); }
      .vl-tag-miss { color: var(--vl-bad) !important; background: rgba(227,119,106,0.14); }
      .vl-tag-neutral { color: var(--vl-brass) !important; background: rgba(224,179,77,0.14); }

      /* Guidance Tracker's Company x Quarter x [+] grid — fixed pixel
       * widths + horizontal scroll instead of the normal shrink-to-fit
       * st.columns() behavior, so quarter columns stay comfortably
       * writable/readable no matter how many accumulate (2026-08-16
       * request). :first-child / :last-child are structural, not a
       * fixed count, so this works regardless of how many quarters
       * exist — column order is always [Company (with its own remove
       * button inside), q1..qN, ➕] with nothing after ➕ (Price/Remove
       * both dropped from being separate trailing columns, same
       * request), so ➕ can keep appending columns rightward forever.
       * Every st.columns() row inside this one container shares the
       * same scroll position since none of them have their own overflow
       * context — only the outer container does.
       *
       * 2026-08-16 (later same day): the grid now paginates quarters
       * 3-at-a-time ("slides", see page_guidance_tracker's PAGE_SIZE), so
       * horizontal scroll is now the fallback, not the norm — bumped the
       * note st.text_area's height (see the row-render loop) since 3
       * visible columns leaves plenty of spare height that used to need
       * rationing across however many quarters had piled up.
       *
       * 2026-08-16 (yet later same day, "increase width of columns as we
       * enough space on the page"): quarter columns switched from a fixed
       * px width to flex: 1 1 0 — they now stretch to fill whatever's
       * left after Company/➕'s fixed widths, instead of leaving a dead
       * gap on wide screens. min-width keeps them readable and is what
       * triggers the horizontal-scroll fallback on narrow ones. */
      .st-key-vl_guidance_grid { overflow-x: auto; padding-bottom: 10px; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap; min-width: max-content; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:first-child {
        flex: 0 0 auto !important; width: 220px !important; min-width: 220px !important; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:last-child {
        flex: 0 0 auto !important; width: 60px !important; min-width: 60px !important; }
      .st-key-vl_guidance_grid div[data-testid="stHorizontalBlock"] > div:not(:first-child):not(:last-child) {
        flex: 1 1 0 !important; min-width: 340px !important; }

      /* Guidance Tracker's Older/Newer pager (2026-08-16, "reduce the
       * size of buttons") — used to stretch full-width via wide columns;
       * these are small, secondary controls so they now size to their
       * text instead. The Add-company button used to get the same
       * treatment, but once its form's width got capped (below) to match
       * the header, a fixed-width button went back to full-width
       * (use_container_width, at the call site) so it doesn't look
       * undersized in the now-narrow form. */
      .st-key-vl_gt_nav button {
        padding: 4px 16px !important; font-size: 13px !important; width: auto !important; }
      /* Add-company form itself capped to ~the "Management Guidance
       * Tracker" subheader's own rendered width (~425px, measured; 480px
       * leaves breathing room) — 2026-08-16, "set width as per header for
       * adding company", same pattern as the password gate's login box:
       * a small utility form reads better aligned under its heading than
       * stretched across the full page. */
      .st-key-vl_gt_add_company { max-width: 480px; }
      .st-key-vl_gt_nav div[data-testid="stHorizontalBlock"] > div:last-child {
        display: flex !important; justify-content: flex-end !important; }

      /* Slim icon buttons (the summary row's Remove) */
      div[data-testid="stHorizontalBlock"] button[kind="secondary"] { padding: 2px 10px; }

      /* Own toggle — flatten the default bordered button box so the
       * ✅/⬜ reads as a plain value like every other column (Bear, EMA,
       * etc.) instead of a UI control (2026-08-16, "doesnt looks same
       * as bear column format"). Scoped via the key-derived class
       * Streamlit adds to the element container (st-key-<key>); "_own_"
       * only ever appears in this button's own key, never Remove's or
       * any other, so this can't leak onto other buttons. */
      div[data-testid="stElementContainer"][class*="_own_"] button {
        background: transparent !important; border: none !important;
        box-shadow: none !important; padding: 2px 0 !important; font-size: 15px !important; }
      div[data-testid="stElementContainer"][class*="_own_"] button:hover {
        background: transparent !important; border: none !important; }

      /* Summary row's company name — a tertiary st.button styled to read
         as a link (opens that company's Detail page) rather than a button,
         replacing the old standalone 🔍 icon column. */
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"] {
        padding: 0; font-weight: 700; font-size: 14.5px; color: var(--vl-ink) !important;
      }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"]:hover { color: var(--vl-accent) !important; }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"] p { text-decoration: underline;
        text-decoration-color: transparent; transition: text-decoration-color 0.15s; }
      div[data-testid="stHorizontalBlock"] button[kind="tertiary"]:hover p { text-decoration-color: currentColor; }

      /* Financial Modelling table's per-cell number_input widgets — made
         to look like inline table cells rather than standalone form
         fields. Critical-input rows (Revenue Growth %/OPM %/Tax %) get a
         brass underline via :has() + the widget's own aria-label. */
      div[data-testid="stNumberInput"] { margin-bottom: 0 !important; }
      div[data-testid="stNumberInput"] input {
        background: transparent !important; border: none !important;
        border-bottom: 1px dashed var(--vl-faint) !important; border-radius: 0 !important;
        /* 13px → 15px (2026-08-16, "increase the font in this table") */
        color: var(--vl-ink) !important; font-size: 15px !important;
        /* center, not right (2026-08-16, "numbers indented are messed,
         * let's keep center") — matches the auto-computed rows below
         * (Revenue/PAT/EPS, already text-align:center) and this grid's
         * only other st.number_input use site, the PE Multiple row, so
         * every value in the grid now lines up on the same axis. */
        text-align: center !important; padding: 2px 4px !important; height: 30px !important;
      }
      div[data-testid="stNumberInput"] button { display: none !important; }
      div[data-testid="stNumberInput"]:has(input[aria-label="Revenue Growth %"]) input,
      div[data-testid="stNumberInput"]:has(input[aria-label="OPM %"]) input,
      div[data-testid="stNumberInput"]:has(input[aria-label="Tax %"]) input {
        border-bottom: 1.5px solid var(--vl-brass) !important;
        background: rgba(224,179,77,0.10) !important; font-weight: 600 !important;
      }

      /* Future Projections grid — one vertical rule after each FY's 3
       * case columns (Base/Bull/Bear), so the 9-column Year×Case grid
       * reads as 3 visually separated FY groups instead of one
       * undifferentiated block. Scoped to this grid's own st.container
       * key (not global) so it can't bleed into the summary table or
       * the fundamentals table, which use unrelated column counts. */
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:nth-child(4),
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:nth-child(7) {
        border-right: 1px solid var(--vl-border);
        padding-right: 6px;
      }

      /* Row labels (Revenue Growth %, Interest Expense Cr, Number of
       * Shares Cr, ...) at default body font size wrap to 2 lines in
       * that narrow first column on real-world (non-maximized) window
       * widths, breaking the "each metric is one row" layout this grid
       * is built around — shrink + force single line so column width
       * math never has to guess the exact pixel budget. */
      .st-key-vl_projections_grid div[data-testid="stHorizontalBlock"] > div:first-child p {
        font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
    </style>
    """, unsafe_allow_html=True)


def render_stat_row(items):
    cards = "".join(f'<div class="vl-stat"><span class="vl-stat-label">{lab}</span>'
                     f'<span class="vl-stat-value">{val}</span></div>' for lab, val in items)
    st.markdown(f'<div class="vl-stat-row">{cards}</div>', unsafe_allow_html=True)


def render_chip_row(chips):
    """chips: list of (label, color, cagr_text, sub_text) tuples."""
    cards = "".join(
        f'<div class="vl-chip" style="--chip-color:{color}"><span class="vl-chip-label">{label} CAGR</span>'
        f'<span class="vl-chip-cagr">{cagr_text}</span><span class="vl-chip-sub">{sub_text}</span></div>'
        for label, color, cagr_text, sub_text in chips
    )
    st.markdown(f'<div class="vl-chip-row">{cards}</div>', unsafe_allow_html=True)


def case_summary_cell_html(h):
    """Summary page's Base/Bull/Bear cell (2026-08-16 request) — FY year
    promoted to a small header line instead of buried at the bottom
    ("set the FY Year to header"; kept per-row rather than in the actual
    page header since different companies can land on different FY years
    for the same case — each one's estimate years are counted from its
    own last reported year, not a shared calendar), then EPS Growth | PE
    | CAGR collapsed onto one compact line in that order ("instead of 2
    lines, start with EPS Growth | PE | CAGR" — previously CAGR was the
    big headline number with growth/PE/price/year stacked below it across
    2 lines; share price dropped here, still on the Detail page).
    "growth" is really the revenue-growth driver that feeds the whole
    model (see headline_cagr's dict) — labeled EPS Growth here since
    that's the number it ultimately drives.
    `h` is a headline_cagr() dict, or None/cagr-less when PE isn't filled
    in yet for any estimate year."""
    if not h or h["cagr"] is None:
        return '<div style="text-align:center;"><span class="vl-sub">fill PE</span></div>'
    cls = "vl-pos" if h["cagr"] >= 0 else "vl-neg"
    # Font sizes bumped 10.5px/12px → 11.5px/14px (2026-08-16, "increase
    # number fonts as we have enough space") — the column has room now
    # that this collapsed to one value line instead of two.
    return (f'<div style="text-align:center;">'
            f'<div style="font-size:11.5px;font-weight:700;color:var(--vl-faint);">FY{h["year"]}</div>'
            f'<div style="font-size:14px;white-space:nowrap;">{fmt_signed(h["growth"], 1)} | {fmt(h["pe"], 1)}x | '
            f'<span class="{cls}" style="font-weight:700;">{fmt_signed(h["cagr"], 1)}</span></div>'
            f'</div>')


def ema_pct_html(price, ema_value):
    """Signed % gap between price and that EMA — sign alone conveys
    above/below (positive = above = bullish), so no separate Y/N letter
    is shown (2026-08-16, "instead of Y or N, let's directly give the
    percentage values" — replaces the earlier Y/N-plus-%-underneath
    version, renamed from ema_yn_html accordingly). "—" when either
    input is missing (e.g. a company added before this existed and not
    yet refreshed, or the EMA fetch failed for that company on last
    refresh — see fetch_one()'s degrade-to-None handling in
    screener_fetch.py)."""
    if price is None or ema_value is None or ema_value == 0:
        return '<div style="text-align:center;"><span class="vl-sub">—</span></div>'
    pct = (price - ema_value) / ema_value * 100
    return pct_value_html(pct)


def pct_value_html(pct):
    """Bare signed %, centered and color-coded — the same rendering
    ema_pct_html ends on, factored out (2026-08-16) so the Summary
    table's new Qtr Sales Gr% column (latest-quarter YoY, see
    render_stock_section) can reuse it without duplicating the markup."""
    if pct is None:
        return '<div style="text-align:center;"><span class="vl-sub">—</span></div>'
    cls = "vl-pos" if pct >= 0 else "vl-neg"
    return f'<div style="text-align:center;"><span class="{cls}" style="font-weight:700;">{fmt_signed(pct)}</span></div>'


def qtr_sales_growth_html(quarter_label, pct):
    """Summary table's Qtr Sales Gr% cell — which quarter as a small
    header line (Q2FY27, see fiscal_quarter_label()), then the YoY %
    below it, same two-line pattern as case_summary_cell_html's FY-year
    mini-header (2026-08-16, "instead of generic name, can we give which
    quarterSalesGrowth like Q2FY27" — a bare % alone didn't say which
    quarter it was for)."""
    if pct is None or not quarter_label:
        return '<div style="text-align:center;"><span class="vl-sub">—</span></div>'
    cls = "vl-pos" if pct >= 0 else "vl-neg"
    return (f'<div style="text-align:center;">'
            f'<div style="font-size:10.5px;font-weight:700;color:var(--vl-faint);">'
            f'{fiscal_quarter_label(quarter_label)}</div>'
            f'<span class="{cls}" style="font-weight:700;">{fmt_signed(pct)}</span></div>')


def hist_cell_html(v, digits, suffix, colorize, bold):
    # font-size:15px (2026-08-16, matching the same bump applied to the
    # Quarterly Results table's own q_cell_html — "add same way header to
    # annual result as well") — this is the annual table's only
    # remaining caller now that quarterly has its own cell renderer.
    text = fmt(v, digits, suffix)
    cls = ""
    if colorize and v is not None:
        cls = "vl-pos" if v >= 0 else "vl-neg"
    weight = "font-weight:700;" if bold else ""
    span = (f'<span class="{cls}" style="font-size:15px;{weight}">{text}</span>' if cls
            else f'<span style="font-size:15px;{weight}">{text}</span>')
    return f'<div style="text-align:right;">{span}</div>'


# ───────────────────────── Summary page ─────────────────────────

def refresh_all_stocks(session_id, show_progress=True):
    """Re-fetches every tracked ticker from Screener.in and saves (which
    also GitHub-syncs, see save_all_stocks). Shared by the manual
    "Refresh all now" button and the once-a-day auto-trigger in
    maybe_auto_refresh() — same code path either way. Returns the count
    refreshed."""
    raw = load_raw_all_stocks()
    tickers = list(raw.keys())
    if not tickers:
        return 0
    progress = st.progress(0.0, text="Refreshing…") if show_progress else None
    for i, t in enumerate(tickers):
        # Small pacing gap between tickers (2026-08-16, "EMA distance
        # missing for many companies") — each fetch_one() already hits
        # 4 endpoints (search, page, 2x chart API) with zero delay
        # between THEM; back-to-back across many tickers with no gap
        # here too compounds that into a burst confirmed to cause
        # transient chart-API failures (silently degrading EMAs to None
        # — see fetch_price_series()'s own retry, added alongside this).
        # Skipped before the FIRST ticker so a single-company refresh
        # (or this loop's very start) isn't slowed down for nothing.
        if i > 0:
            time.sleep(0.4)
        data, err = fetch_one(t, session_id)
        if not err:
            raw[t] = merge_fetched(raw.get(t), data)
        if progress:
            progress.progress((i + 1) / len(tickers), text=f"Refreshed {t}")
    save_all_stocks(raw)
    if progress:
        progress.empty()
    return len(tickers)


def refresh_prices_only(session_id, show_progress=True):
    """Lightweight sibling of refresh_all_stocks() — current_price/
    pe_ratio/market_cap_cr/week52_high only, via fetch_price_only()
    (see its docstring), merged in without touching P&L/EMA/quarters/
    owned (see merge_price_only()). 2026-08-16 request: "2 refresh for
    fundamental data & prices alone, as prices do need daily updates" —
    fundamentals don't change day to day, so the once-a-day auto-refresh
    (maybe_auto_refresh()) now calls THIS instead of the full
    refresh_all_stocks(), which stays manual-only (its own "🔄 Refresh
    all now" button on the Summary page) for updating P&L/quarterly/EMA.
    Same pacing between tickers as the full refresh, for the same
    rate-limit reason — roughly half the requests per ticker here, but
    still worth not bursting across many tickers at once."""
    raw = load_raw_all_stocks()
    tickers = list(raw.keys())
    if not tickers:
        return 0
    progress = st.progress(0.0, text="Refreshing prices…") if show_progress else None
    for i, t in enumerate(tickers):
        if i > 0:
            time.sleep(0.4)
        data, err = fetch_price_only(t, session_id)
        if not err:
            raw[t] = merge_price_only(raw.get(t), data)
        if progress:
            progress.progress((i + 1) / len(tickers), text=f"Refreshed {t}")
    save_all_stocks(raw)
    if progress:
        progress.empty()
    return len(tickers)


def retrieve_companies(ticker_input, session_id):
    """Fetches every ticker in a comma-separated input string (2026-08-16
    request: "can we put multiple companies together" — previously a
    multi-ticker paste like "mtar, windlas, mcx, bbox" got sent to
    Screener's search API as one literal string and failed outright:
    "could not resolve 'MTAR, WINDLAS, MCX, BBOX'"). Saves each success
    into all_stocks.json (merged via merge_fetched(), not overwritten)
    and appends it to the Guidance Tracker's tracked list — same
    behavior a single-ticker Retrieve/Add already had, just looped.
    Small pacing delay between tickers, same rate-limit reasoning as
    refresh_all_stocks()'s.

    Shared by section_retrieve() (Summary page) and the Guidance
    Tracker's own Add-company form — both had near-identical single-
    ticker fetch+save+track logic before this, now just call this once.

    Returns (successes, failures): successes is a list of fetched data
    dicts, failures a list of (typed_ticker, error_message) tuples."""
    tickers = [t.strip() for t in ticker_input.split(",") if t.strip()]
    successes, failures = [], []
    raw = load_raw_all_stocks()
    tracker = load_guidance_tracker()
    tracked = tracker.setdefault("tracked", [])
    for i, t in enumerate(tickers):
        if i > 0:
            time.sleep(0.4)
        data, err = fetch_one(t.upper(), session_id)
        if err:
            failures.append((t, err))
            continue
        raw[data["ticker"]] = merge_fetched(raw.get(data["ticker"]), data)
        if data["ticker"] not in tracked:
            tracked.append(data["ticker"])
        successes.append(data)
    save_all_stocks(raw)
    if successes:
        save_guidance_tracker(tracker)
    return successes, failures


def maybe_auto_refresh():
    """Once per calendar day, silently refreshes every tracked ticker's
    PRICE the first time the app is opened that day — no button click
    needed, no growth/PE inputs required. Price-only (2026-08-16, "2
    refresh for fundamental data & prices alone, as prices do need daily
    updates") — fundamentals (annual P&L, Quarterly Results, EMAs) don't
    change day to day, so running the full fetch_one() here daily was
    wasted load (and the extra EMA chart-API requests are the confirmed
    source of transient failures under bulk load, see
    fetch_price_series()'s retry). Full fundamentals refresh is now
    manual-only, via the Summary page's own "🔄 Refresh all now" button.

    The "day is done" flag (data/last_refresh.json) is GitHub-synced
    like everything else, so whichever device happens to open the app
    first each day satisfies it for every device, not just that one.

    Blocking: this runs synchronously before the page renders, so the
    first visitor of the day waits on N sequential Screener.in fetches
    (roughly half of what the old full-refresh version cost, per ticker).
    Fine for a personal watchlist of a handful of tickers; would need
    revisiting (e.g. a real scheduler) if the tracked list grows large.

    Gated by st.session_state so it only ever attempts once per browser
    session — main() (and therefore this) re-runs on every Streamlit
    rerun, i.e. every widget interaction, not just the initial load."""
    if st.session_state.get("auto_refresh_checked"):
        return
    st.session_state["auto_refresh_checked"] = True

    session_id = get_session_id()  # optional — fetch_one() works fine with None
    today = date.today().isoformat()
    if load_last_refresh().get("date") == today:
        return

    raw = load_raw_all_stocks()
    if raw:
        with st.spinner(f"Refreshing prices for {len(raw)} tracked companies today…"):
            n = refresh_prices_only(session_id, show_progress=False)
        if n:
            st.toast(f"💹 Auto-refreshed prices for {n} companies for {today}")
    save_last_refresh({"date": today})


def staleness_reason(stock):
    """None when fine, else a short reason string for the Summary row's
    stale-data badge (2026-08-16 request). Two independent triggers:
    missing EMA (the exact silent-failure mode hit for CONCORDBIO/AVALON
    — worth flagging immediately regardless of how recent the fetch
    timestamp is), and a fundamentals fetch older than STALE_DAYS days
    (or never done at all, for a company added before this field
    existed)."""
    if any(stock.get(k) is None for k in ("ema20d", "ema50d", "ema33w")):
        return "EMA data missing — refresh all"
    fund_at = stock.get("fundamentals_fetched_at")
    if not fund_at:
        # Field didn't exist before 2026-08-16 — every company tracked
        # before today lacks it and WAS fetched fully at some point, so
        # flagging all of them "never refreshed" on the day this ships
        # would be noise, not signal. Silent until their next full
        # refresh backfills this; the EMA check above still catches the
        # cases that actually matter regardless of this field's age.
        return None
    try:
        age_days = (datetime.now() - datetime.strptime(fund_at, "%Y-%m-%d %H:%M:%S")).days
    except ValueError:
        return None
    if age_days > STALE_DAYS:
        return f"Fundamentals {age_days}d old — refresh all"
    return None


def render_stock_section(stocks, scenarios, section_key, empty_msg):
    """One sortable table — Company/Price/PE/Qtr Sales Gr%/EMA%/Base/Bull/Bear/Own/🗑️ —
    for a pre-filtered subset of all_stocks. Factored out of page_summary
    (2026-08-16, "divide the summary to 2 parts, stocks I own and other
    as tracking") so it can render twice, once per section, without
    duplicating this whole block. section_key namespaces sort state and
    every widget key so the two tables' sort/interactions never collide
    (e.g. sorting "Stocks I Own" by Price doesn't touch "Tracking"'s
    sort, and both can have a row with the same ticker key-safe)."""
    if not stocks:
        st.markdown(f'<div class="vl-empty">{empty_msg}</div>', unsafe_allow_html=True)
        return

    # GRID_CASES (module-level, excludes "mgmt") drives the case columns —
    # see its definition for why. EMA_COLS: signed % gap between price and
    # that EMA, sign alone conveying above/below (see ema_pct_html()) —
    # Close-based via Screener's own chart API, not OHLC4 (explicit
    # trade-off, 2026-08-16, over a second data source like yfinance that
    # has no coverage for SME/small-cap names this app otherwise supports).
    EMA_COLS = [("ema20d", "20D"), ("ema50d", "50D"), ("ema33w", "33W")]
    # Qtr Sales Gr% inserted after P/E (2026-08-16, "since there is lot of
    # space in summary header... can we add latest quarterly sales
    # growth%") — latest-quarter YoY, the last entry of q_revenue_growth_pct
    # (oldest-to-newest, same order as "quarters"; see screener_fetch.py).
    # None for companies not yet refreshed since Quarterly Results shipped.
    # Upside/Downside to Bull inserted after P/E, before Qtr Sales Gr%
    # (2026-08-16, "Base-case upside/downside %", then "use bull case, as
    # I did conservative bull case inputs provided already" — Bull is
    # this user's realistic case, not an aspirational stretch case, so
    # it's the one to measure upside against, not Base. The Bull cell
    # further right already shows an *annualized* CAGR to its target
    # year, but that buries the plain "how far is today's price from
    # that target" number a user actually scans first; this reuses
    # headline_cagr()'s already-computed share_price, no new fetching
    # needed).
    ema_start = 5
    col_widths = [2.2, 0.8, 0.6, 0.85, 0.75] + [0.5] * len(EMA_COLS) + [1.05, 1.05, 1.05, 0.5, 0.55]
    n_ema_cols = len(EMA_COLS)
    case_start = ema_start + n_ema_cols
    n_case_cols = len(GRID_CASES)
    own_col = case_start + n_case_cols
    remove_col = own_col + 1

    # ── Sort — every column clickable (2026-08-16 request). Sort key per
    # row is computed once up front (needed before we know render order
    # anyway), keyed by a stable column id independent of display label so
    # a case-label rename (CASE_LABEL) can't silently break a saved sort.
    # None-valued cells (company not yet refreshed, EMA fetch failure,
    # case CAGR needing a PE fill first) always sort to the bottom
    # regardless of direction — split them out rather than relying on
    # Python's sort+reverse, which would put them first on a "desc" sort.
    rows = []
    for ticker, stock in stocks.items():
        price = stock.get("current_price")
        case_cagr, case_headline = {}, {}
        for case in GRID_CASES:
            h = headline_cagr(stock, get_case_state(scenarios, stock, ticker, case))
            case_headline[case] = h
            case_cagr[case] = h["cagr"] if h and h["cagr"] is not None else None
        q_growth = stock.get("q_revenue_growth_pct")
        q_labels = stock.get("quarters")
        qtr_sales_g = q_growth[-1] if q_growth else None
        qtr_sales_label = q_labels[-1] if q_labels else None
        # Upside is measured off the Bull case, not Base (2026-08-16,
        # "use bull case, as I did conservative bull case inputs
        # provided already" — for this user Bull is already the
        # realistic/conservative scenario, so Base would understate it).
        bull_h = case_headline.get("bull")
        upside = None
        if bull_h and bull_h.get("share_price") is not None and price:
            upside = (bull_h["share_price"] / price - 1) * 100
        rows.append({"ticker": ticker, "stock": stock, "price": price, "pe": stock.get("pe_ratio"),
                      "case_headline": case_headline, "case_cagr": case_cagr, "upside": upside,
                      "qtr_sales_g": qtr_sales_g, "qtr_sales_label": qtr_sales_label,
                      "stale": staleness_reason(stock)})

    def sort_value(row, col):
        if col == "name":
            return row["stock"]["name"].lower()
        if col == "price":
            return row["price"]
        if col == "pe":
            return row["pe"]
        if col == "qtr_sales_g":
            return row["qtr_sales_g"]
        if col == "upside":
            return row["upside"]
        if col.startswith("ema_"):
            ema_key = col[len("ema_"):]
            ema_val = row["stock"].get(ema_key)
            return (row["price"] - ema_val) / ema_val * 100 if row["price"] is not None and ema_val else None
        return row["case_cagr"].get(col)

    sort_col_key, sort_dir_key = f"{section_key}_sort_col", f"{section_key}_sort_dir"
    sort_col = st.session_state.get(sort_col_key)
    sort_dir = st.session_state.get(sort_dir_key, "asc")
    if sort_col:
        present = [r for r in rows if sort_value(r, sort_col) is not None]
        missing = [r for r in rows if sort_value(r, sort_col) is None]
        present.sort(key=lambda r: sort_value(r, sort_col), reverse=(sort_dir == "desc"))
        rows = present + missing

    def header_text(col_id, label):
        # Arrow appended inline, next to the label, for EVERY column
        # (2026-08-16, "instead lets make sort icon to appear to next of
        # text instead of below.. for all cols" — reverses the previous
        # two passes, which first moved it to a separate centered line
        # under centered columns only to fix a real visual-centering skew
        # there, then made that consistent across all columns; this
        # trades that back for inline placement everywhere per explicit
        # request. If the centering skew on centered columns (Qtr Sales
        # Gr%/EMA/Base/Bull/Bear — see the "again indent issue" fix
        # history above) turns out to bother again, that's the fix to
        # revisit.)
        if col_id != sort_col:
            return label
        return f"{label} {'▼' if sort_dir == 'desc' else '▲'}"

    # Column id, display label, center-aligned (matches its value below).
    # Base/Bull/Bear centered too (2026-08-16 fix) — case_summary_cell_html
    # centers its FY-year/EPS-growth-PE-CAGR line, so a left-aligned
    # header above it had the same header/value mismatch already fixed
    # once for the EMA columns.
    header_defs = ([("name", "Company", False), ("price", "Price", True), ("pe", "P/E", True),
                     ("upside", "Upside", True), ("qtr_sales_g", "Qtr Sales Gr%", True)]
                    + [(f"ema_{k}", lbl, True) for k, lbl in EMA_COLS]
                    + [(c, CASE_LABEL[c].replace(" Case", ""), True) for c in GRID_CASES])

    # Header buttons reuse the existing tertiary "link text" styling (see
    # inject_css()'s button[kind="tertiary"] rule) instead of new CSS —
    # clicking toggles asc/desc on the same column, or switches to a new
    # one at asc. use_container_width only on the EMA/Qtr-Sales-Gr%/case
    # columns so their (already-centered) button text lines up with the
    # centered value below; the rest stay left-aligned, sized to their
    # own text, like before.
    with st.container(key=f"vl_summary_header_{section_key}"):
        header = st.columns(col_widths)
        header_help = {"upside": "Current price vs. the Bull case's target price today (not annualized — "
                                  "see the Bull column further right for the annualized CAGR to that target)"}
        for i, (col_id, label, centered) in enumerate(header_defs):
            with header[i]:
                if st.button(header_text(col_id, label), key=f"{section_key}_sort_{col_id}", type="tertiary",
                             use_container_width=centered, help=header_help.get(col_id)):
                    if sort_col == col_id:
                        st.session_state[sort_dir_key] = "desc" if sort_dir == "asc" else "asc"
                    else:
                        st.session_state[sort_col_key] = col_id
                        st.session_state[sort_dir_key] = "asc"
                    st.rerun()
        # Own is now a plain button column, same as Remove — no more
        # checkbox to CSS-match, so this header is just centered text
        # like every other non-sortable/simple header (2026-08-16,
        # "better make it a column same as others columns").
        header[own_col].markdown(
            '<div style="text-align:center;font-size:11px;color:var(--vl-faint);">Own</div>',
            unsafe_allow_html=True)
        header[remove_col].write("")  # Remove column — no header, not sortable
    st.markdown('<hr style="margin:2px 0 8px;border-color:var(--vl-border);">', unsafe_allow_html=True)

    for row in rows:
        ticker, stock = row["ticker"], row["stock"]
        # vertical_alignment="center" (2026-08-16 — reverted from a
        # "bottom" + hidden-spacer-line approach that turned out to look
        # worse in practice than intended, per direct feedback: "not good
        # looking, lets revert back to indent center on column level for
        # each cell"). Simple per-cell centering, no extra markup needed.
        cols = st.columns(col_widths, vertical_alignment="center")
        # Name itself opens the Detail page (replaces the old standalone 🔍
        # icon column, 2026-08-15) — styled via the tertiary-button CSS
        # above to read as a link, not a button.
        if cols[0].button(stock["name"], key=f"{section_key}_open_{ticker}", type="tertiary",
                           help=f"Open {stock['name']}"):
            st.session_state["_jump_to"] = ticker
            st.rerun()
        cols[0].markdown(f"<span class='vl-sub' style='display:block;margin-top:-10px;'>{ticker}</span>",
                          unsafe_allow_html=True)
        if row["stale"]:
            # Stale-data badge (2026-08-16 request) — only rendered when
            # there's actually something to flag, silent otherwise so a
            # healthy row doesn't get cluttered with a "✓ fresh" line.
            cols[0].markdown(
                f'<span title="{row["stale"]}" style="display:block;font-size:10.5px;'
                f'color:var(--vl-brass);margin-top:-8px;">⚠️ {row["stale"]}</span>',
                unsafe_allow_html=True)
        # Centered, like every other value column (2026-08-16, "spacing
        # is uneven between cols" — Price/P/E were the only two still on
        # plain left-aligned st.write(); the real column gap is a fixed
        # 16px everywhere (verified), but Price/P/E's left-hugging text
        # against neighboring centered columns read as ragged/uneven
        # whitespace even though the grid itself was consistent).
        cols[1].markdown(f'<div style="text-align:center;">₹{fmt(row["price"])}</div>', unsafe_allow_html=True)
        cols[2].markdown(f'<div style="text-align:center;">{fmt(row["pe"], 1)}x</div>', unsafe_allow_html=True)
        cols[3].markdown(pct_value_html(row["upside"]), unsafe_allow_html=True)
        cols[4].markdown(qtr_sales_growth_html(row["qtr_sales_label"], row["qtr_sales_g"]), unsafe_allow_html=True)
        for i, (ema_key, _) in enumerate(EMA_COLS):
            cols[ema_start + i].markdown(ema_pct_html(row["price"], stock.get(ema_key)), unsafe_allow_html=True)
        for i, case in enumerate(GRID_CASES):
            cols[case_start + i].markdown(case_summary_cell_html(row["case_headline"][case]),
                                           unsafe_allow_html=True)
        # Own toggle — a plain button, not st.checkbox() (2026-08-16,
        # "better make it a column same as others columns" — the
        # checkbox's internal DOM structure needed increasingly elaborate
        # CSS (:has() on ancestor containers, then matching the header's
        # own centering method to it) and still measured "tilted right"
        # on the user's actual browser despite pixel-perfect measurements
        # here every time. The Remove button right next to it never had
        # this problem, so this column now works exactly like that one —
        # a plain st.button(use_container_width=True), which Streamlit
        # centers natively with no custom CSS needed at all.
        # Moves the row to the other section on next rerun, same as the
        # checkbox did; key namespaces by section+ticker so a stale
        # widget in the "wrong" section (from before a toggle moved this
        # row) can't leak its value in.
        owned = stock.get("owned", False)
        if cols[own_col].button("✅" if owned else "⬜", key=f"{section_key}_own_{ticker}_btn",
                                 help="Click to mark as owned/not owned", use_container_width=True):
            raw = load_raw_all_stocks()
            if ticker in raw:
                raw[ticker]["owned"] = not owned
                save_all_stocks(raw)
            st.rerun()
        if cols[remove_col].button("🗑️", key=f"{section_key}_remove_{ticker}",
                                    help=f"Remove {stock['name']} from tracking", use_container_width=True):
            raw = load_raw_all_stocks()
            raw.pop(ticker, None)
            save_all_stocks(raw)
            st.rerun()


def page_summary(all_stocks, scenarios):
    if not all_stocks:
        if st.button("📋 Management Guidance Tracker →",
                      help="Track quarterly management guidance and Beat/Neutral/Miss per company"):
            st.session_state["_view"] = "guidance_tracker"
            st.rerun()
        st.markdown('<div class="vl-empty">No companies yet — retrieve one from Screener.in below.</div>',
                    unsafe_allow_html=True)
        return

    # All 3 action buttons in ONE row, sized to their own text and packed
    # left (2026-08-16, "buttons are at random places.. can we improve
    # ui styles") — previously Guidance Tracker sat alone as a bare
    # full-width element, then a separate st.columns([1, 1]) row below it
    # split the *entire* page width in half for just two buttons, landing
    # "Refresh all now" oddly out at the page's horizontal center instead
    # of next to its sibling. A narrow ratio per button (sized ~to its
    # own label) plus one trailing spacer column reads as one deliberate
    # button bar instead of three independently-placed elements.
    btn_col1, btn_col2, btn_col3, _spacer = st.columns([1.7, 1.3, 1.1, 3.4])
    with btn_col1:
        if st.button("📋 Management Guidance Tracker →",
                      help="Track quarterly management guidance and Beat/Neutral/Miss per company"):
            st.session_state["_view"] = "guidance_tracker"
            st.rerun()
    # Two separate refresh actions (2026-08-16, "2 refresh for
    # fundamental data & prices alone, as prices do need daily updates")
    # — Prices is the fast/cheap one (~half the requests per company,
    # see fetch_price_only()) for something that's genuinely stale by
    # the next trading session; Fundamentals (P&L/Quarterly Results/EMA)
    # doesn't change day to day so it stays a deliberate, separate action.
    with btn_col2:
        if st.button("💹 Refresh prices only", help="Fast — price/PE/market cap/52W high only, no P&L/quarterly/EMA"):
            n = refresh_prices_only(get_session_id())
            st.success(f"Refreshed prices for {n} companies.")
            st.rerun()
    with btn_col3:
        if st.button("🔄 Refresh all now", help="Full refresh — re-fetches P&L, Quarterly Results, and EMAs too (slower)"):
            n = refresh_all_stocks(get_session_id())
            st.success(f"Refreshed {n} companies.")
            st.rerun()

    # Split into two independently-sortable tables (2026-08-16, "divide
    # the summary to 2 parts, stocks I own and other as tracking") —
    # "owned" is a local-only flag (not from Screener), toggled per-row
    # via the Own checkbox in render_stock_section and preserved across
    # refreshes by merge_fetched(). Ordering preserved within each half
    # (dict insertion order from all_stocks, same as before the split).
    owned_stocks = {t: s for t, s in all_stocks.items() if s.get("owned")}
    tracking_stocks = {t: s for t, s in all_stocks.items() if not s.get("owned")}

    st.subheader(f"📦 Stocks I Own ({len(owned_stocks)})")
    render_stock_section(owned_stocks, scenarios, "owned",
                          "No owned stocks yet — check the Own box on a company below to move it here.")

    st.divider()

    st.subheader(f"🔭 Tracking ({len(tracking_stocks)})")
    render_stock_section(tracking_stocks, scenarios, "tracking", "Nothing being tracked right now.")


# ───────────────────────── Management Guidance Tracker page ─────────────────────────

def page_guidance_tracker(all_stocks):
    """Company (static, left, with its own remove button) x Quarter
    (grows sideways via the ➕ column at the end, middle/right) grid —
    track what management guided each quarter, free-text notes plus a
    Beat/Neutral/Miss tag, added 2026-08-16 per explicit request. No
    Price column here (dropped on request, 2026-08-16 — already on the
    Summary page, redundant here, and kept ➕ from being anything but the
    literal last column so it can keep appending indefinitely). Quarter
    columns and this page's own tracked-company list are both
    user-controlled (add/remove/rename), not automatic — see
    load_guidance_tracker()'s docstring for why "tracked" here is
    deliberately separate from all_stocks.json's full company set even
    though adding a company here still fetches into that same shared
    store (no duplicated price data, only tracked-here membership)."""
    if st.button("← Back to Summary", key="gt_back"):
        st.session_state["_view"] = "summary"
        st.rerun()

    st.subheader("📋 Management Guidance Tracker")
    st.caption("Company (left) × Quarter (grows rightward — ➕ at the end adds another).")

    tracker = load_guidance_tracker()
    quarters = tracker.setdefault("quarters", [])
    tracked = tracker.setdefault("tracked", [])
    cells = tracker.setdefault("cells", {})

    # ── Add a company (fetches into the shared all_stocks.json, same as
    # Retrieve on Summary — only this page's "tracked" membership is new) ──
    # Keyed container (2026-08-16, "reduce the size of buttons") so the Add
    # button shrinks to its text instead of stretching the full column —
    # scoped to this container so it can't touch the Retrieve form's own
    # primary button on the Summary page, which stays full-width on purpose.
    with st.container(key="vl_gt_add_company"):
        with st.form("gt_add_company", clear_on_submit=True):
            # Stacked, not side-by-side (2026-08-16) — the old [6, 1]
            # column split wrapped "Add" onto two lines ("Ad"/"d") once
            # this form's width got capped to match the header; full-width
            # rows top-to-bottom fit that width comfortably instead (same
            # fix already applied to the Retrieve form).
            # Comma-separated multi-ticker support (2026-08-16, "can we
            # put multiple companies together") — see
            # retrieve_companies() for the actual fetch loop.
            new_ticker = st.text_input("Add a company to this tracker",
                                        placeholder="e.g. TITAN, or MTAR, WINDLAS, MCX for several at once").strip()
            add_co_submitted = st.form_submit_button("Add", type="primary", use_container_width=True)
    if add_co_submitted and new_ticker:
        with st.spinner(f"Fetching {new_ticker} from Screener.in…"):
            successes, failures = retrieve_companies(new_ticker, get_session_id())
        if successes:
            names = ", ".join(f"{d['name']} ({d['ticker']})" for d in successes)
            msg = f"✅ Added: {names}."
            if failures:
                msg += " ⚠️ Couldn't fetch: " + "; ".join(f"{t}: {err}" for t, err in failures)
            st.toast(msg)  # persists across the rerun below, unlike st.success
            st.rerun()
        else:
            failed_desc = "; ".join(f"{t}: {err}" for t, err in failures)
            st.error(f"Couldn't fetch: {failed_desc}")
    elif add_co_submitted:
        st.warning("Type a ticker or company name first.")

    if not tracked:
        st.markdown('<div class="vl-empty">No companies tracked here yet — add one above.</div>',
                    unsafe_allow_html=True)
        return

    st.divider()

    # Quarters paginate 3-at-a-time ("slides") instead of all piling up in
    # one horizontally-scrolling row (explicit request, 2026-08-16, later
    # same day as the fixed-width change below) — with only 3 columns ever
    # on screen at once, each can afford to be wider/taller than when an
    # arbitrary number had to share the row. gt_slide is the 0-indexed
    # slide number; session_state persists it across reruns (tag/note
    # edits rerun this page) but NOT across a fresh page load, so opening
    # the tracker always starts on the most recent slide.
    PAGE_SIZE = 3
    total_slides = max(1, -(-len(quarters) // PAGE_SIZE))  # ceil div
    if "gt_slide" not in st.session_state:
        st.session_state["gt_slide"] = total_slides - 1
    st.session_state["gt_slide"] = max(0, min(st.session_state["gt_slide"], total_slides - 1))
    slide = st.session_state["gt_slide"]
    slide_start = slide * PAGE_SIZE
    visible_quarters = quarters[slide_start:slide_start + PAGE_SIZE]

    if total_slides > 1:
        # Keyed container, same reason as vl_gt_add_company above — Older/
        # Newer read as small paging controls, not full-width action
        # buttons, so they no longer stretch to fill their column.
        with st.container(key="vl_gt_nav"):
            nav = st.columns([1, 3, 1])
            with nav[0]:
                if st.button("◀ Older", key="gt_slide_prev", disabled=slide == 0):
                    st.session_state["gt_slide"] = slide - 1
                    st.rerun()
            with nav[1]:
                st.markdown(f"<div style='text-align:center;color:var(--vl-muted);padding-top:6px;'>"
                            f"Quarters {slide_start + 1}–{min(slide_start + PAGE_SIZE, len(quarters))} of "
                            f"{len(quarters)}</div>", unsafe_allow_html=True)
            with nav[2]:
                if st.button("Newer ▶", key="gt_slide_next", disabled=slide >= total_slides - 1):
                    st.session_state["gt_slide"] = slide + 1
                    st.rerun()

    # Fixed pixel widths + horizontal scroll (explicit request, 2026-08-16:
    # "set the width to each quarter to at least sizeable to write and
    # read easily") instead of Streamlit's normal st.columns() behavior,
    # which proportionally SHRINKS every column to fit the container —
    # scroll is now just a fallback for narrow viewports since pagination
    # above caps this at 3 quarter columns regardless of how many exist
    # overall. Scoped to this one container (.st-key-vl_guidance_grid, see
    # inject_css()) so it can't affect the Summary table or the
    # Fundamentals/Projections grids on the Detail page, which all still
    # want the normal shrink-to-fit behavior. Structural :first-child /
    # :nth-last-child selectors (not nth-child(N) counting from the
    # start) so the CSS doesn't need to know how many quarter columns
    # are visible — Company stays wide-fixed on the left, ➕ stays
    # narrow-fixed on the right, and every visible quarter in between
    # gets the same comfortable fixed width. The relative ratios still
    # passed to st.columns() below are moot once this CSS's
    # `flex: 0 0 auto` overrides them — kept only because st.columns()
    # requires *some* ratio list matching the column count.
    with st.container(key="vl_guidance_grid"):
        # ➕ is the LAST column, full stop (explicit request, 2026-08-16:
        # "we dont need price col at end... let + button be end of the
        # col, so it can keep appending the cols") — Price dropped from
        # this grid entirely (already on the Summary page, redundant
        # here), and the per-row remove button moved into the Company
        # cell itself so nothing trails after ➕ either. New quarters
        # just keep extending the list rightward with ➕ always at the
        # tip of the full list — adding one auto-jumps to the slide that
        # now contains it, since it's by definition the most recent.
        col_widths = [2.0] + [1.4] * len(visible_quarters) + [0.5]
        add_q_col = 1 + len(visible_quarters)  # == last index

        # ── Header row ── (remove button stacked under the label, not a
        # nested st.columns(), so the fixed-width CSS above — which
        # targets every stHorizontalBlock inside this container — has no
        # nested row to accidentally also force wide)
        header = st.columns(col_widths)
        header[0].markdown("**Company**")
        for vi, q in enumerate(visible_quarters):
            i = slide_start + vi  # real index into the full quarters list
            with header[1 + vi]:
                # Keyed by the quarter's own label, not its index — an
                # index-based key would collide with stale session_state
                # left over from whatever quarter used to sit at that
                # position before an earlier removal shifted the list,
                # showing/renaming the wrong quarter's label.
                new_label = st.text_input("Quarter label", value=q, key=f"gt_qlabel_{q}",
                                           label_visibility="collapsed")
                if new_label != q and new_label.strip() and new_label not in quarters:
                    quarters[i] = new_label.strip()
                    cells_for_q = {t: cells[t].pop(q) for t in cells if q in cells[t]}
                    for t, cell in cells_for_q.items():
                        cells[t][new_label.strip()] = cell
                    save_guidance_tracker(tracker)
                    st.rerun()
                if st.button("✕ remove", key=f"gt_rmq_{q}", help=f"Remove the \"{q}\" column (all companies)"):
                    quarters.remove(q)
                    for t in cells:
                        cells[t].pop(q, None)
                    save_guidance_tracker(tracker)
                    st.rerun()
        with header[add_q_col]:
            st.write("")
            if st.button("➕", key="gt_add_quarter_btn", help="Add a new quarter column"):
                n = len(quarters) + 1
                label = f"Quarter {n}"
                while label in quarters:
                    n += 1
                    label = f"Quarter {n}"
                quarters.append(label)
                save_guidance_tracker(tracker)
                st.session_state["gt_slide"] = max(0, -(-len(quarters) // PAGE_SIZE) - 1)
                st.rerun()
        st.markdown('<hr style="margin:2px 0 8px;border-color:var(--vl-border);">', unsafe_allow_html=True)

        # ── One row per tracked company ──
        for ticker in list(tracked):
            stock = all_stocks.get(ticker)
            row_cells = cells.setdefault(ticker, {})
            cols = st.columns(col_widths)
            with cols[0]:
                st.markdown(
                    f"**{stock['name'] if stock else ticker}**  \n<span class='vl-sub'>{ticker}</span>",
                    unsafe_allow_html=True)
                if st.button("🗑️ remove", key=f"gt_rm_{ticker}", help=f"Remove {ticker} from this tracker only "
                             "(doesn't touch the main company list)"):
                    tracked.remove(ticker)
                    save_guidance_tracker(tracker)
                    st.rerun()

            for vi, q in enumerate(visible_quarters):
                cell = row_cells.setdefault(q, {"note": "", "tag": ""})
                with cols[1 + vi]:
                    tag = st.selectbox("Tag", GUIDANCE_TAGS,
                                        index=GUIDANCE_TAGS.index(cell["tag"]) if cell["tag"] in GUIDANCE_TAGS else 0,
                                        key=f"gt_{ticker}_{q}_tag", label_visibility="collapsed")
                    # Always render the badge line, hidden (not omitted)
                    # when there's no tag yet — reserves the same height
                    # either way, so every row's note box starts at the
                    # same y position regardless of which cells happen to
                    # have a tag set (misalignment reported 2026-08-16:
                    # tagged cells' notes sat lower than untagged ones').
                    badge_html = (f"<span class='vl-badge {GUIDANCE_TAG_CLASS[tag]}'>{tag}</span>" if tag
                                  else "<span class='vl-badge' style='visibility:hidden;'>—</span>")
                    st.markdown(badge_html, unsafe_allow_html=True)
                    # height bumped 90→140 (2026-08-16, same request as the
                    # column-width bump above) — 3-per-slide pagination
                    # freed up enough width that taller notes read better
                    # than wider-but-shorter ones.
                    note = st.text_area("Note", value=cell["note"], key=f"gt_{ticker}_{q}_note",
                                         label_visibility="collapsed", height=140,
                                         placeholder="Guidance / commentary…")
                    if tag != cell["tag"] or note != cell["note"]:
                        row_cells[q] = {"note": note, "tag": tag}
                        save_guidance_tracker(tracker)


# ───────────────────────── Detail page (fundamentals) ─────────────────────────

def page_detail(stock, ticker, scenarios):
    # Slim sticky header (2026-08-16 request: "static header on scrolling
    # down... just like screener") — Screener pins a compact name+price
    # bar once you scroll past the full header; this stays pinned from
    # the start instead (simpler, no scroll-position JS needed, and still
    # answers the actual need: which company/price you're looking at
    # stays visible no matter how far down the page — annual table,
    # quarterly table, projections grid — you've scrolled). Separate from
    # the full subheader+stat row below, which stays as normal (non-
    # sticky) page content; duplicating the full 4-stat row here would
    # make the pinned bar too tall, unlike Screener's own slim version.
    #
    # Rendered FIRST, before anything else — the bar is position:fixed
    # (out of flow) so it always overlays whatever's underneath in that
    # viewport region regardless of DOM order; the only fix is making
    # sure nothing else renders ABOVE this point that would end up
    # hidden under it (confirmed earlier: with this below the Back
    # button, the spacer couldn't retroactively push the button down —
    # it had already been placed higher up before the bar/spacer ever
    # entered the page).
    st.markdown(
        f'<div class="vl-sticky-header">'
        f'<span class="vl-sticky-name">{stock["name"]} <span class="vl-sub">({ticker})</span></span>'
        f'<span class="vl-sticky-detail">'
        f'<span class="vl-sticky-sep">·</span>₹{fmt(stock.get("current_price"))}'
        f'<span class="vl-sticky-sep">·</span>PE {fmt(stock.get("pe_ratio"), 1)}x</span>'
        f'</div>'
        # Spacer — the bar above is position:fixed (out of flow), so
        # without this the page's own content renders right under
        # Streamlit's toolbar and the fixed bar just overlaps on top of
        # it. Height matches the bar's own rendered height (measured
        # live at the current 28px/H3 name size: ~58px; 64px leaves a
        # hair of margin) — shrunk alongside the "reduce font size"
        # follow-up request from the taller H1-sized version's 96px.
        f'<div style="height:64px;"></div>',
        unsafe_allow_html=True)

    # Back button folded into the sticky bar itself (2026-08-16, "back to
    # summary now to be added to static header") — a real st.button (not
    # part of the markdown above, which is plain HTML with no Python
    # callback) fixed-positioned over the bar's own reserved left margin
    # via the keyed container's CSS (.st-key-vl_sticky_back_btn), so it
    # visually reads as part of the header despite being a separate
    # Streamlit element underneath in the DOM.
    with st.container(key="vl_sticky_back_btn"):
        if st.button("← Back to Summary"):
            st.session_state["_jump_to"] = None
            st.rerun()

    st.subheader(f"{stock['name']} ({ticker})")
    render_stat_row([
        ("Price", f"₹{fmt(stock.get('current_price'))}"),
        ("P/E", f"{fmt(stock.get('pe_ratio'), 1)}x"),
        ("Mkt Cap", f"₹{fmt(stock.get('market_cap_cr'))} Cr"),
        ("52W High", f"₹{fmt(stock.get('week52_high'))}"),
    ])

    if st.button(f"🔄 Refresh {ticker} now"):
        with st.spinner("Fetching…"):
            data, err = fetch_one(ticker, get_session_id())
        if err:
            st.error(err)
        else:
            raw = load_raw_all_stocks()
            raw[ticker] = merge_fetched(raw.get(ticker), data)
            save_all_stocks(raw)
            st.success("Refreshed.")
            st.rerun()

    # GRID_CASES, not CASES — drops the Management Case chip (2026-08-16,
    # "we dnt have mgmt guidance, lets remove"), same exclusion already
    # applied to the Summary page and this page's own Projections grid/
    # Key Assumptions (see GRID_CASES's definition). Any Management Case
    # scenario data already saved is untouched, just not shown here.
    chips = []
    for case in GRID_CASES:
        state = get_case_state(scenarios, stock, ticker, case)
        h = headline_cagr(stock, state)
        color = CASE_COLOR[case]
        if h and h["cagr"] is not None:
            chips.append((CASE_LABEL[case], color, fmt_signed(h["cagr"], 1), f"₹{fmt(h['share_price'])} · FY{h['year']}"))
        else:
            chips.append((CASE_LABEL[case], color, "—", "fill PE to compute"))
    render_chip_row(chips)

    st.divider()
    # Reflects the actual per-stock fetch result, not assumed consolidated
    # — screener_fetch.py falls back to standalone whenever consolidated
    # has too few years (e.g. a company whose consolidated reporting only
    # started recently despite decades of standalone history), so this
    # varies stock to stock and the label needs to say which it got.
    basis = "consolidated" if stock.get("consolidated") else "standalone"
    # H2 + caption (2026-08-16, "add same way header to annual result as
    # well" — matching Qtr Results' own st.header() + st.caption() pair,
    # in place of the plain st.caption()-only line this used to be).
    st.header("Annual Results")
    st.caption(f"Screener.in, {basis} — annual Profit & Loss")

    n = len(stock["years"])
    hist_col_widths = [2.2] + [1] * n

    def hist_row(label, vals, digits=0, suffix="", colorize=False, bold=False):
        cols = st.columns(hist_col_widths)
        cols[0].markdown(("**" + label + "**") if bold else label)
        for j, v in enumerate(vals):
            cols[1 + j].markdown(hist_cell_html(v, digits, suffix, colorize, bold), unsafe_allow_html=True)

    hdr = st.columns(hist_col_widths)
    hdr[0].markdown("**Financial Year**")
    for j, y in enumerate(stock["years"]):
        hdr[1 + j].markdown(f"<div style='text-align:right;color:var(--vl-faint);font-size:13.5px;"
                             f"font-weight:700;'>{y}</div>", unsafe_allow_html=True)

    hist_row("Revenue Cr", stock["revenue"], bold=True)
    hist_row("Revenue Growth %", stock["revenue_growth_pct"], 1, "%", colorize=True)
    hist_row("Expenses Cr", stock["expenses"])
    hist_row("Operating Profit Cr", stock["operating_profit"], bold=True)
    hist_row("OPM %", stock["opm_pct"], 1, "%", colorize=True)
    hist_row("Other Income Cr", stock["other_income"])
    hist_row("Interest Expense Cr", stock["interest"])
    hist_row("Depreciation Cr", stock["depreciation"])
    hist_row("PBT Cr", stock["pbt"], bold=True)
    hist_row("Tax %", stock["tax_pct"], 1, "%")
    hist_row("PAT Cr", stock["net_profit"], bold=True)
    hist_row("PAT Growth %", stock["pat_growth_pct"], 1, "%", colorize=True)
    hist_row("Number of Shares Cr", stock["shares_cr"], 3)
    hist_row("EPS ₹", stock["eps"], 2, bold=True)

    # Quarterly Results (2026-08-16 request: "can we also pull in
    # quarterly results") — same table shape as the annual one above, just
    # quarter-labeled columns; growth% is YoY (same-quarter-prior-year,
    # date-matched — see screener_fetch.py's module docstring for why not
    # a fixed -4 offset), not QoQ, to avoid seasonal noise. Missing
    # entirely (old cached fetch from before this existed, or the section
    # wasn't found/parseable for this company) degrades to just not
    # showing the table — same best-effort posture as EMAs.
    if stock.get("quarters"):
        st.divider()
        # H2 (2026-08-16, "add a mid page header H2 as Qtr Results") —
        # st.header(), not the st.subheader() (H3) every other section
        # label in this app uses, so this reads as a step up/more
        # prominent than those, matching the same request's "font is
        # very small, increase" for the rest of this table too.
        st.header("Qtr Results")
        st.caption(f"Screener.in, {basis} — last {len(stock['quarters'])} quarters")
        nq = len(stock["quarters"])
        q_col_widths = [2.2] + [1] * nq

        def q_cell_html(v, digits, suffix, colorize, bold):
            # Own (bigger) cell renderer, not hist_cell_html — that one's
            # shared with the annual table above, which wasn't part of
            # this "too small" complaint; sizing it up here only avoids
            # quietly resizing the annual table along with it.
            text = fmt(v, digits, suffix)
            cls = ""
            if colorize and v is not None:
                cls = "vl-pos" if v >= 0 else "vl-neg"
            weight = "font-weight:700;" if bold else ""
            span = (f'<span class="{cls}" style="font-size:15px;{weight}">{text}</span>' if cls
                    else f'<span style="font-size:15px;{weight}">{text}</span>')
            return f'<div style="text-align:right;">{span}</div>'

        def q_row(label, vals, digits=0, suffix="", colorize=False, bold=False):
            cols = st.columns(q_col_widths)
            cols[0].markdown(("**" + label + "**") if bold else label)
            for j, v in enumerate(vals):
                cols[1 + j].markdown(q_cell_html(v, digits, suffix, colorize, bold), unsafe_allow_html=True)

        qhdr = st.columns(q_col_widths)
        qhdr[0].markdown("**Quarter**")
        for j, q in enumerate(stock["quarters"]):
            qhdr[1 + j].markdown(f"<div style='text-align:right;color:var(--vl-faint);font-size:13.5px;"
                                  f"font-weight:700;'>{q}</div>", unsafe_allow_html=True)

        q_row("Sales Cr", stock.get("q_revenue", []), bold=True)
        q_row("Sales Growth % (YoY)", stock.get("q_revenue_growth_pct", []), 1, "%", colorize=True)
        q_row("Expenses Cr", stock.get("q_expenses", []))
        q_row("Operating Profit Cr", stock.get("q_operating_profit", []), bold=True)
        q_row("OPM %", stock.get("q_opm_pct", []), 1, "%", colorize=True)
        q_row("Other Income Cr", stock.get("q_other_income", []))
        q_row("Interest Expense Cr", stock.get("q_interest", []))
        q_row("Depreciation Cr", stock.get("q_depreciation", []))
        q_row("PBT Cr", stock.get("q_pbt", []), bold=True)
        q_row("Tax %", stock.get("q_tax_pct", []), 1, "%")
        q_row("Net Profit Cr", stock.get("q_net_profit", []), bold=True)
        q_row("PAT Growth % (YoY)", stock.get("q_pat_growth_pct", []), 1, "%", colorize=True)
        q_row("EPS ₹", stock.get("q_eps", []), 2, bold=True)

    st.divider()
    render_projections_grid(stock, ticker, scenarios)


def render_projections_grid(stock, ticker, scenarios):
    """Base/Bull/Bear/Management × all 3 estimate years flattened into one
    grid — explicit correction, 2026-08-15: an earlier version repeated a
    full 4-case header + ~11 rows per year (3 separate blocks), which
    meant scrolling past a lot of vertical space that the page's width
    wasn't using ("input boxes are very wide enough" — plenty of spare
    horizontal room). One metric per row, with all Year×Case combinations
    as columns in that same row, uses the width instead of the scroll."""
    st.subheader("🎯 Future Projections & CAGR — all cases, all years, one grid")

    # GRID_CASES (module-level) excludes "mgmt" here too — see its
    # definition for why. The persist loop below only writes back
    # drivers for the cases actually rendered in this grid.

    guidance = load_guidance(ticker)
    if guidance:
        st.info(f"📋 **Guidance-seeded** — Base/Bull/Bear Revenue Growth % pre-filled from management guidance "
                f"research where available (as of {guidance.get('as_of', 'unknown')}). Edit any case freely.",
                icon="📋")

    case_states = {c: get_case_state(scenarios, stock, ticker, c) for c in GRID_CASES}

    EDITABLE_FIELDS = ["revGrowth", "opm", "tax", "other_income", "interest", "depreciation", "shares"]
    FIELD_STEP = {"revGrowth": 0.5, "opm": 0.5, "tax": 0.5, "other_income": 1.0,
                  "interest": 1.0, "depreciation": 1.0, "shares": 0.01}
    FIELD_DIGITS = {"revGrowth": 1, "opm": 1, "tax": 1, "other_income": 1,
                     "interest": 1, "depreciation": 1, "shares": 3}
    FIELD_LABEL = {"revGrowth": "Revenue Growth %", "opm": "OPM %", "tax": "Tax %",
                   "other_income": "Other Income Cr", "interest": "Interest Expense Cr",
                   "depreciation": "Depreciation Cr", "shares": "Number of Shares Cr"}

    def widget_key(case, field, i):
        return f"{ticker}_{case}_{field}_{i}"

    # Two-pass, same trick as before but across all rendered cases: Pass
    # 1 reads every case's current widget values straight out of
    # session_state (a case's "Revenue Cr" needs that case's own Revenue
    # Growth %, whose input widget renders in the same grid) and
    # computes each case's full 3-year model up front. Pass 2 renders.
    effective = {}
    for case in GRID_CASES:
        state = case_states[case]
        eff_list = []
        for i in range(N_EST_YEARS):
            dr = state["drivers"][i]
            eff = {f: st.session_state.get(widget_key(case, f, i), as_float(dr[f])) for f in EDITABLE_FIELDS}
            eff["pe"] = st.session_state.get(f"{ticker}_{case}_pe_{i}", as_float(dr["pe"]))
            eff_list.append(eff)
        effective[case] = eff_list
    models = {case: compute_model(stock, {"drivers": effective[case]}) for case in GRID_CASES}

    last_year = int(stock["years"][-1].split(" ")[1])
    n_cases = len(GRID_CASES)
    grid_widths = [1.5] + [0.95] * (N_EST_YEARS * n_cases)

    def col_index(i, c):
        return 1 + i * n_cases + c

    def header_row():
        cols = st.columns(grid_widths)
        cols[0].markdown("&nbsp;", unsafe_allow_html=True)
        for i in range(N_EST_YEARS):
            year = last_year + i + 1
            for c, case in enumerate(GRID_CASES):
                cols[col_index(i, c)].markdown(
                    f"<div style='text-align:center;font-size:11.5px;line-height:1.3;'>"
                    f"<b>FY{year}</b><br><span style='color:{CASE_COLOR[case]};font-weight:700;'>"
                    f"{CASE_LABEL[case].replace(' Case', '')}</span></div>", unsafe_allow_html=True)

    def input_row(label, field):
        cols = st.columns(grid_widths)
        cols[0].markdown(label)
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                with cols[col_index(i, c)]:
                    st.number_input(FIELD_LABEL[field], value=effective[case][i][field],
                                     step=FIELD_STEP[field], format=f"%.{FIELD_DIGITS[field]}f",
                                     key=widget_key(case, field, i), label_visibility="collapsed")

    def computed_row(label, field, digits=0, suffix=""):
        cols = st.columns(grid_widths)
        cols[0].markdown(f"*{label}*")
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                v = models[case][i][field]
                cols[col_index(i, c)].markdown(
                    # 13.5px → 16px (2026-08-16, "increase revenue pat
                    # eps font size" — a further bump on top of the
                    # earlier 12px→13.5px pass, now matching/slightly
                    # above the number_input rows' 15px).
                    f"<div style='text-align:center;color:var(--vl-muted);font-style:italic;font-size:16px;'>"
                    f"{fmt(v, digits, suffix)}</div>", unsafe_allow_html=True)

    # Keyed container so the FY-separator CSS rule (nth-child on this
    # grid's own st.columns() rows) can't bleed into any other table on
    # the page — see inject_css()'s .st-key-vl_projections_grid rule.
    with st.container(key="vl_projections_grid"):
        header_row()
        input_row("Revenue Growth %", "revGrowth")
        input_row("OPM %", "opm")
        input_row("Tax %", "tax")
        input_row("Other Income Cr", "other_income")
        input_row("Interest Expense Cr", "interest")
        input_row("Depreciation Cr", "depreciation")
        input_row("Number of Shares Cr", "shares")
        computed_row("Revenue Cr", "revenue")
        computed_row("PAT Cr", "pat")
        computed_row("EPS ₹", "eps", 2)

        pe_cols = st.columns(grid_widths)
        pe_cols[0].markdown("**PE Multiple**")
        for i in range(N_EST_YEARS):
            for c, case in enumerate(GRID_CASES):
                # PE mirrors Revenue Growth % live until the user types
                # their own PE (2026-08-16: "when user sets growth to
                # revenue PE should take same number" — a PEG-of-1
                # starting point, e.g. 25% growth → 25.0x, that a user is
                # still free to override). Can't do this with a plain
                # `value=` kwarg — Streamlit only honours `value` on a
                # widget's very first mount in a session, so typing
                # Revenue Growth % on year 2 after year 1 already exists
                # wouldn't budge an already-rendered PE box. Instead we
                # pre-seed st.session_state[pe_key] ourselves before the
                # widget's created, tracking our own last-auto-set value
                # (pe_auto_key) to tell "still on auto" apart from "user
                # typed something, possibly identical by coincidence,
                # leave it alone from now on" on every subsequent rerun.
                pe_key = f"{ticker}_{case}_pe_{i}"
                pe_auto_key = f"{ticker}_{case}_pe_auto_{i}"
                auto_pe = effective[case][i]["revGrowth"]
                if pe_key not in st.session_state:
                    # First render this session — respect a genuinely
                    # saved/manual PE (don't clobber a deliberate choice
                    # just because a fresh page load re-seeds session
                    # state); only fall back to mirroring growth when
                    # nothing was saved at all.
                    saved_pe = case_states[case]["drivers"][i].get("pe")
                    st.session_state[pe_key] = saved_pe if saved_pe is not None else auto_pe
                elif st.session_state[pe_key] == st.session_state.get(pe_auto_key):
                    st.session_state[pe_key] = auto_pe
                st.session_state[pe_auto_key] = auto_pe
                with pe_cols[col_index(i, c)]:
                    st.number_input("PE Multiple", step=0.5, format="%.1f", key=pe_key,
                                     label_visibility="collapsed", placeholder="PE")

        cagr_cols = st.columns(grid_widths)
        cagr_cols[0].markdown("**CAGR**")
        for i in range(N_EST_YEARS):
            year = last_year + i + 1
            for c, case in enumerate(GRID_CASES):
                eps = models[case][i]["eps"]
                pe_val = st.session_state.get(f"{ticker}_{case}_pe_{i}")
                share_price = eps * pe_val if eps is not None and pe_val else None
                cagr = cagr_for(stock["current_price"], share_price, days_until(year))
                # Bigger + share price dropped (2026-08-16, "increase the
                # font in this table, and mainly CAGR, remove share price
                # below CAGR value") — this is the grid's headline number,
                # share price is still visible via the chips near the top
                # of the page, so no information's actually lost.
                with cagr_cols[col_index(i, c)]:
                    if cagr is None:
                        st.markdown('<div style="text-align:center;"><span class="vl-sub">—</span></div>',
                                    unsafe_allow_html=True)
                    else:
                        cls = "vl-pos" if cagr >= 0 else "vl-neg"
                        st.markdown(f'<div style="text-align:center;"><span class="{cls}" '
                                    f'style="font-size:19px;font-weight:700;">{fmt_signed(cagr, 1)}</span></div>',
                                    unsafe_allow_html=True)

    st.markdown('<div style="font-size:11px;color:var(--vl-faint);margin-top:6px;">'
                'plain = editable estimate (carried forward by default) · <i>italic</i> = auto-computed</div>',
                unsafe_allow_html=True)

    # ── Persist any edits across the cases rendered here (Mgmt's saved
    # state, if any, is untouched — it has no widgets in this grid) ──
    for case in GRID_CASES:
        state = case_states[case]
        new_drivers = []
        for i in range(N_EST_YEARS):
            nd = {f: st.session_state.get(widget_key(case, f, i)) for f in EDITABLE_FIELDS}
            nd["pe"] = st.session_state.get(f"{ticker}_{case}_pe_{i}")
            new_drivers.append(nd)
        if new_drivers != state["drivers"]:
            state["drivers"] = new_drivers
            set_case_state(scenarios, ticker, case, state)

    # ── Key assumptions — one narrow text area per case ──
    st.divider()
    st.caption("Key Assumptions")
    assum_cols = st.columns(len(GRID_CASES))
    for c, case in enumerate(GRID_CASES):
        with assum_cols[c]:
            st.markdown(f"<span style='color:{CASE_COLOR[case]};font-weight:600;font-size:12.5px;'>"
                        f"{CASE_LABEL[case]}</span>", unsafe_allow_html=True)
            val = st.text_area(f"{case} assumptions", value=case_states[case]["assumptions"], height=110,
                                key=f"{ticker}_{case}_assumptions", label_visibility="collapsed", max_chars=5000)
            if val != case_states[case]["assumptions"]:
                case_states[case]["assumptions"] = val
                set_case_state(scenarios, ticker, case, case_states[case])
            if st.button("Clear estimates", key=f"{ticker}_{case}_clear", use_container_width=True):
                scenarios.get(ticker, {}).pop(case, None)
                save_scenarios(scenarios)
                st.rerun()


# ───────────────────────── Retrieve ─────────────────────────

def section_retrieve(all_stocks):
    # Pinned top-right beside the title instead of sitting at the bottom
    # of the page below the whole stock table (2026-08-16, "move to top
    # right corner") — caller (main()) puts this in the narrow column of
    # a st.columns([3, 1]) row, so no divider needed above it here anymore.
    # Width also capped to ~its own subheader's rendered width (~370px;
    # 430px leaves breathing room), same treatment as the password gate
    # and Guidance Tracker's Add-company form ("set width as per header
    # for adding company... for this field as well").
    st.markdown('<style>.st-key-vl_retrieve_form { max-width: 430px; }</style>', unsafe_allow_html=True)
    with st.container(key="vl_retrieve_form"):
        st.subheader("📥 Retrieve from Screener")

        with st.form("retrieve_form", clear_on_submit=True):
            # Stacked, not side-by-side (2026-08-16) — the old [4, 1]
            # column split wrapped "Retrieve" onto two lines once this
            # form moved into the narrow top-right column; full-width
            # rows top-to-bottom fit that width comfortably instead.
            # Comma-separated multi-ticker support (2026-08-16, "can we
            # put multiple companies together") — see
            # retrieve_companies() for the actual fetch loop.
            new_ticker = st.text_input("NSE/BSE ticker or company name",
                                        placeholder="e.g. TITAN, or MTAR, WINDLAS, MCX for several at once").strip()
            submitted = st.form_submit_button("Retrieve", type="primary", use_container_width=True)

    if submitted and new_ticker:
        with st.spinner(f"Fetching {new_ticker} from Screener.in…"):
            successes, failures = retrieve_companies(new_ticker, get_session_id())
        # Also added to the Guidance Tracker's own row list (2026-08-16
        # request: "if a company is added to summary screen, same should
        # be available on management guidance tracker page also") —
        # reverses the earlier "tracked list is deliberately separate"
        # design (see load_guidance_tracker()'s docstring); handled
        # inside retrieve_companies() now, same as before per-ticker.
        # Still one-directional by construction: removing a company from
        # the tracker (its own 🗑️) doesn't touch this page's list, only
        # the reverse.
        if successes:
            names = ", ".join(f"{d['name']} ({d['ticker']})" for d in successes)
            msg = f"✅ Retrieved: {names}."
            if failures:
                msg += " ⚠️ Couldn't fetch: " + "; ".join(f"{t}: {err}" for t, err in failures)
            # st.toast, not st.success — this still reruns right after
            # (to show the new companies in the table), and a plain
            # st.success would vanish with nothing to show for it since
            # it renders in the pass that's about to be discarded; a
            # toast persists across the rerun the way the auto-refresh
            # notification elsewhere in this file already relies on.
            st.toast(msg)
            # Only jump straight to the Detail page for the original
            # single-ticker case (one company, no failures) — jumping
            # anywhere specific doesn't make sense once multiple
            # companies just got added at once.
            if len(successes) == 1 and not failures:
                st.session_state["_jump_to"] = successes[0]["ticker"]
            st.rerun()
        else:
            failed_desc = "; ".join(f"{t}: {err}" for t, err in failures)
            st.error(f"Couldn't fetch: {failed_desc}")
    elif submitted:
        st.warning("Type a ticker or company name first.")


# ───────────────────────── Main ─────────────────────────

def main():
    inject_css()  # runs before the password gate too, so the lock screen gets the same dark theme
    if not check_password():
        return

    maybe_auto_refresh()  # before load_all_stocks(), so a same-day refresh renders fresh, not stale

    all_stocks = load_all_stocks()
    scenarios = load_scenarios()
    jump_to = st.session_state.get("_jump_to")

    if jump_to and jump_to in all_stocks:
        # No "🧮 Valuation Ledger" app-brand title here (unlike the other
        # two branches below) — 2026-08-16: once the sticky header got
        # bumped to H1 size, this title rendered right underneath it and
        # got covered regardless of any spacer, since the sticky bar is
        # position:fixed and always overlays whatever's in that viewport
        # region no matter how page_detail()'s own content is padded.
        # Redundant anyway now — the sticky bar already carries the
        # company name permanently, and page_detail() has its own
        # subheader with the same name+ticker right below it.
        page_detail(all_stocks[jump_to], jump_to, scenarios)
        return

    if st.session_state.get("_view") == "guidance_tracker":
        st.title("🧮 Valuation Ledger")
        page_guidance_tracker(all_stocks)
        return

    # Summary page only: title/caption share the top row with Retrieve
    # instead of Retrieve sitting at the very bottom of the page below the
    # whole stock table (2026-08-16, "move to top right corner").
    title_col, retrieve_col = st.columns([3, 1])
    with title_col:
        st.title("🧮 Valuation Ledger")
        st.caption("Summary — retrieve companies live from Screener.in")
        # Company count (2026-08-16 request) — moved to its own line below
        # the caption, big font, rather than folded into the small caption
        # text. n_companies from all_stocks (every retrieved company), not
        # the Guidance Tracker's separate "tracked" subset (see
        # load_guidance_tracker's docstring for that distinction).
        n_companies = len(all_stocks)
        st.markdown(f'<div style="font-size:28px;font-weight:700;color:var(--vl-ink);margin-top:2px;">'
                    f'{n_companies} compan{"y" if n_companies == 1 else "ies"} tracked</div>',
                    unsafe_allow_html=True)
    with retrieve_col:
        section_retrieve(all_stocks)

    page_summary(all_stocks, scenarios)


if __name__ == "__main__":
    main()
