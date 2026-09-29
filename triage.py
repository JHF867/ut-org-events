"""
triage.py - keeps the Apify account list lean. The pipeline runs this for you;
you only ever edit data/orgs.csv.

Every account lands in one of three groups:
  active    posted in the last ACTIVE_DAYS days  -> checked on its normal tier schedule
  sleeper   quiet longer than that                -> checked once every SLEEPER_EVERY days
  dead      "not_found" twice, or private         -> skipped until you fix its handle
Accounts with no history yet count as active, so nothing is skipped by mistake.

Manual use (optional):
  python triage.py report      prints the counts and rewrites data/account_status.csv
"""
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACTIVE_DAYS = 21
SLEEPER_EVERY = 28
DEAD_AFTER_NOT_FOUND = 2

HERE = Path(__file__).parent
DATA = HERE / "data"
STATE_FILE = DATA / "accounts_state.json"
ORGS_FILE = DATA / "orgs.csv"
STATUS_FILE = DATA / "account_status.csv"
NOW = datetime.now(timezone.utc)


def load_orgs():
    """handle -> row dict (handle, tier, name, purpose, school, membership)."""
    with open(ORGS_FILE, newline="", encoding="utf-8") as f:
        return {r["handle"].strip().lower(): r for r in csv.DictReader(f) if r.get("handle")}


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def handle_from(row):
    """The account WE asked for (so collab posts are credited to our org)."""
    for key in ("inputUrl", "url"):
        v = row.get(key)
        if v and "instagram.com/" in v and "/p/" not in v and "/reel/" not in v:
            return v.rstrip("/").split("/")[-1].lower()
    if row.get("username") and row.get("error"):
        return row["username"].lower()
    return None


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _newer(a, b):
    if not a:
        return b
    if not b:
        return a
    return a if parse_ts(a) >= parse_ts(b) else b


def update_rows(rows):
    """Fold Apify rows (post or profile scraper) into the saved state."""
    orgs = load_orgs()
    state = load_state()
    today = NOW.isoformat(timespec="seconds")
    seen = set()
    for row in rows:
        h = handle_from(row)
        if not h or h not in orgs:
            continue
        s = state.setdefault(h, {"last_post": None, "not_found": 0, "private": False, "business": None})
        s.setdefault("first_checked", today)
        s["last_checked"] = today
        seen.add(h)
        err = row.get("error")
        if err == "not_found":
            s["not_found"] += 1
            continue
        if err:
            continue
        s["not_found"] = 0
        if "latestPosts" in row or "postsCount" in row:
            s["private"] = bool(row.get("private"))
            s["business"] = row.get("isBusinessAccount")
            for post in row.get("latestPosts") or []:
                s["last_post"] = _newer(s["last_post"], post.get("timestamp"))
        elif row.get("timestamp"):
            s["last_post"] = _newer(s["last_post"], row["timestamp"])
    save_state(state)
    return len(seen)


def status(h, state):
    s = state.get(h)
    if not s:
        return "unknown"
    if s.get("not_found", 0) >= DEAD_AFTER_NOT_FOUND or s.get("private"):
        return "dead"
    lp = parse_ts(s.get("last_post"))
    if lp:
        return "active" if NOW - lp <= timedelta(days=ACTIVE_DAYS) else "sleeper"
    fc = parse_ts(s.get("first_checked"))
    if fc and NOW - fc >= timedelta(days=ACTIVE_DAYS):
        return "sleeper"
    return "unknown"


def sleeper_due(h, state):
    lc = parse_ts(state.get(h, {}).get("last_checked"))
    return lc is None or NOW - lc >= timedelta(days=SLEEPER_EVERY)


def select(kind):
    """Handles to check. kind: 'tier1' (Wed/Fri), 'monday' (both tiers), 'sleepers' (monthly check)."""
    orgs = load_orgs()
    state = load_state()
    out = []
    for h, row in orgs.items():
        st = status(h, state)
        if kind == "tier1" and row.get("tier") == "Tier 1" and st in ("active", "unknown"):
            out.append(h)
        elif kind == "monday" and st in ("active", "unknown"):
            out.append(h)
        elif kind == "sleepers" and st == "sleeper" and sleeper_due(h, state):
            out.append(h)
    return sorted(out)


REASON = {
    "active": "Posted in the last {d} days. Checked every scheduled run.",
    "unknown": "Not enough history yet. Checked every scheduled run until we know more.",
    "sleeper": "No posts in over {d} days. Checked once every {m} days instead of every run.",
    "dead": "Not found twice, or private. Not checked. Fix the handle in data/orgs.csv to bring it back.",
}


def report(quiet=False):
    orgs = load_orgs()
    state = load_state()
    rows, c = [], Counter()
    warn = 0
    for h, org in sorted(orgs.items()):
        st = status(h, state)
        c[st] += 1
        s = state.get(h, {})
        why = REASON[st].format(d=ACTIVE_DAYS, m=SLEEPER_EVERY)
        if st != "dead" and s.get("not_found", 0) >= 1:
            warn += 1
            why += " WARNING: not found on the last check; one more miss and it stops being checked."
        rows.append({"handle": h, "org": org.get("name", ""), "tier": org.get("tier", ""), "status": st,
                     "last_post_seen": (s.get("last_post") or "")[:10],
                     "last_checked": (s.get("last_checked") or "")[:10], "why": why})
    with open(STATUS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    summary = {"checked_every_run": c["active"] + c["unknown"], "active": c["active"], "unsorted": c["unknown"],
               "monthly": c["sleeper"], "not_checked": c["dead"], "warnings": warn, "total": len(orgs)}
    if not quiet:
        print(f"ACCOUNT STATUS ({summary['total']} accounts)")
        print(f"  Checked every run : {summary['checked_every_run']} ({c['active']} active, {c['unknown']} not sorted yet)")
        print(f"  Checked monthly   : {c['sleeper']} (quiet {ACTIVE_DAYS}+ days)")
        print(f"  Not checked       : {c['dead']} (not found twice or private)")
        if warn:
            print(f"  Warning           : {warn} not found once (marked WARNING in account_status.csv)")
    return summary


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        print(__doc__)
