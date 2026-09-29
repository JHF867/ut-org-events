"""
pipeline.py - pulls new Instagram posts from UT student orgs, has Claude turn them
into events (with a free-food flag), and rebuilds the website + calendar feeds.

GitHub Actions runs this on a schedule. You don't need to run it yourself.

Settings you might change live in the SETTINGS block below.
Environment variables (set by the GitHub workflow):
  APIFY_TOKEN, ANTHROPIC_API_KEY   your keys (GitHub secrets)
  TEST_ACCOUNTS   if > 0, only check this many accounts (cheap test run)
  RUN_KIND        'monday' (all accounts) or 'tier1'; blank = pick by weekday
"""
import base64
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import triage

# ---------------------------------------------------------------- SETTINGS
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "apify/instagram-scraper")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_IMAGES_PER_POST = 2          # flyer + one carousel slide
MAX_POSTS_PER_RUN = 700          # safety cap on Claude spend per run
BATCH_CHUNK = 120                # posts per Claude batch (keeps upload size sane)
KEEP_PAST_DAYS = 2               # show events that ended up to 2 days ago
SITE_NAME = "Campus Events + Free Food"
# -------------------------------------------------------------------------

CT = ZoneInfo("America/Chicago")
HERE = Path(__file__).parent
DATA = HERE / "data"
DOCS = HERE / "docs"
SEEN_FILE = DATA / "seen_posts.json"
EVENTS_FILE = DATA / "events.json"
SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")

SYSTEM_PROMPT = """You read Instagram posts from University of Texas at Austin student organizations and record what each post announces.

You get the org name, when the post went up (Central time), the caption, the location tag, collaborators, and flyer images when there are any. Flyers often hold the date, time and room, so read them carefully.

Call record_post exactly once.

Rules:
- is_event = true only for a specific upcoming gathering people can attend (meeting, social, speaker, workshop, tabling, performance, volunteer shift, game, info session). Recaps, thank-yous, member spotlights, merch, polls and general promos are false.
- Resolve relative dates ("today", "tonight", "tomorrow", "this Thursday", "next week Monday") from the post time. Dates as YYYY-MM-DD, times as 24-hour HH:MM, Central time.
- Recurring events ("every Wednesday"): use the next occurrence after the post time and say it recurs in notes.
- If a post lists several events, record the soonest upcoming one and mention the others in notes.
- free_food = true only if food is given out at no cost at the event: "food provided", "lunch on us", "we'll have snacks", "free pizza", "refreshments", catered, potluck the org hosts.
- free_food = false when you pay or bring it: fundraisers, profit shares, "mention us at checkout", "bring snacks", dinner at a restaurant, bake sales, ticketed dinners.
- food_confidence: "stated" (food clearly offered), "likely" (vague, like "refreshments" or a big social with no food named), "none".
- members_only = true if it says members only, invite only, or requires dues/application to attend.
- Use null for anything not stated. Never guess a date, time or room. Buildings: keep UT codes as written (WCP 2.110, GDC, SAC)."""

TOOL = {
    "name": "record_post",
    "description": "Record what this Instagram post announces.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_event": {"type": "boolean"},
            "title": {"type": ["string", "null"], "description": "Short event name, no org name"},
            "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "start_time": {"type": ["string", "null"], "description": "HH:MM 24-hour, Central"},
            "end_time": {"type": ["string", "null"], "description": "HH:MM 24-hour, Central"},
            "location": {"type": ["string", "null"]},
            "free_food": {"type": "boolean"},
            "food_details": {"type": ["string", "null"], "description": "What food, any limits (first 50 people)"},
            "food_confidence": {"type": "string", "enum": ["stated", "likely", "none"]},
            "members_only": {"type": "boolean"},
            "cost": {"type": ["string", "null"], "description": "Entry fee or ticket price if any"},
            "rsvp_link": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"], "description": "One short line: recurring, other dates, caveats"},
        },
        "required": ["is_event", "title", "date", "start_time", "location", "free_food", "food_confidence", "members_only"],
    },
}


# ---------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def now_ct():
    return datetime.now(CT)


# ---------------------------------------------------------------- 1. decide who to check
def plan_runs():
    test_n = int(os.getenv("TEST_ACCOUNTS") or 0)
    kind = (os.getenv("RUN_KIND") or "").strip() or ("monday" if now_ct().weekday() == 0 else "tier1")
    handles = triage.select(kind)
    if test_n > 0:
        log(f"TEST MODE: checking only {test_n} accounts, looking back 7 days.")
        return kind, [(handles[:test_n], "7 days", 3)]
    runs = [(handles, "4 days" if kind == "monday" else "3 days", 8)]
    if kind == "monday":
        sleepers = triage.select("sleepers")
        if sleepers:
            runs.append((sleepers, f"{triage.SLEEPER_EVERY} days", 1))
    return kind, runs


# ---------------------------------------------------------------- 2. scrape
def scrape(handles, window, limit):
    if os.getenv("DRY_RUN_FILE"):                       # local testing without Apify
        rows = json.loads(Path(os.environ["DRY_RUN_FILE"]).read_text())
        wanted = set(handles)
        return [r for r in rows if triage.handle_from(r) in wanted]
    from apify_client import ApifyClient
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    if "post-scraper" in APIFY_ACTOR:
        run_input = {"username": handles, "onlyPostsNewerThan": window, "skipPinnedPosts": True, "resultsLimit": limit}
    else:
        run_input = {"directUrls": [f"https://www.instagram.com/{h}/" for h in handles], "resultsType": "posts",
                     "onlyPostsNewerThan": window, "skipPinnedPosts": True, "resultsLimit": limit,
                     "addParentData": False}
    log(f"Apify: checking {len(handles)} accounts (look back {window}, up to {limit} posts each)...")
    run = client.actor(APIFY_ACTOR).call(run_input=run_input)

    def field(obj, snake, camel):  # works with both older (dict) and newer (object) apify-client versions
        if obj is None:
            return None
        return obj.get(camel) if isinstance(obj, dict) else getattr(obj, snake, None)

    status = field(run, "status", "status")
    if status != "SUCCEEDED":
        msg = field(run, "status_message", "statusMessage") or ""
        raise SystemExit(f"Apify run did not succeed ({status}). {msg}\n"
                         "If it mentions a usage or credit limit, upgrade Apify to the Starter plan.")
    rows = [dict(r) for r in client.dataset(field(run, "default_dataset_id", "defaultDatasetId")).iterate_items()]
    log(f"Apify: {len(rows)} rows returned.")
    return rows


# ---------------------------------------------------------------- 3. images
def image_urls(post):
    urls = [post.get("displayUrl")]
    for child in post.get("childPosts") or []:
        urls.append(child.get("displayUrl"))
    out = []
    for u in urls:
        if u and u not in out:
            out.append(u)
    return out[:MAX_IMAGES_PER_POST]


def fetch_image(url):
    try:
        from PIL import Image
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((1000, 1000))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # expired link, network hiccup: carry on with the caption
        log(f"  image skipped ({type(e).__name__})")
        return None


# ---------------------------------------------------------------- 4. Claude
def build_message(post, org):
    posted = triage.parse_ts(post["timestamp"]).astimezone(CT)
    collabs = [c.get("username") for c in post.get("coauthorProducers") or [] if c.get("username")]
    text = (f"Org: {org.get('name') or org['handle']} (@{org['handle']})\n"
            f"Posted: {posted:%A, %B %-d, %Y at %-I:%M %p} Central\n"
            f"Location tag: {post.get('locationName') or 'none'}\n"
            f"Collaborators: {', '.join(collabs) or 'none'}\n"
            f"Post type: {post.get('type')}\n\nCaption:\n{post.get('caption') or '(no caption)'}")
    content = []
    if not os.getenv("NO_IMAGES"):
        for url in image_urls(post):
            b64 = fetch_image(url)
            if b64:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    content.append({"type": "text", "text": text})
    return {"model": CLAUDE_MODEL, "max_tokens": 700, "system": SYSTEM_PROMPT, "tools": [TOOL],
            "tool_choice": {"type": "tool", "name": "record_post"},
            "messages": [{"role": "user", "content": content}]}


def tool_input(message):
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    return None


def fake_extract(params):
    """Local testing only: a crude keyword stand-in for Claude."""
    import re
    text = params["messages"][0]["content"][-1]["text"].lower()
    cap = text.split("caption:", 1)[1]
    ev = bool(re.search(r"join us|meeting|gbm|social|tonight|today|speaker|workshop|night", cap))
    food = bool(re.search(r"food will be provided|on us|snacks|pizza|provided", cap)) and "bring snacks" not in cap
    return {"is_event": ev, "title": cap.strip().split("\n")[0][:60] or None, "date": None, "start_time": None,
            "location": None, "free_food": food, "food_confidence": "stated" if food else "none", "members_only": False}


def extract(posts_params):
    """posts_params: {shortCode: params}. Returns {shortCode: result dict}."""
    if not posts_params:
        return {}
    if os.getenv("FAKE_CLAUDE"):
        return {k: fake_extract(p) for k, p in posts_params.items()}
    import anthropic
    client = anthropic.Anthropic()
    results = {}
    use_batch = int(os.getenv("TEST_ACCOUNTS") or 0) == 0
    if not use_batch:
        for sc, params in posts_params.items():
            try:
                results[sc] = tool_input(client.messages.create(**params))
            except Exception as e:
                log(f"  Claude error on {sc}: {e}")
        return results
    items = list(posts_params.items())
    for i in range(0, len(items), BATCH_CHUNK):
        chunk = items[i:i + BATCH_CHUNK]
        batch = client.messages.batches.create(requests=[{"custom_id": sc, "params": p} for sc, p in chunk])
        log(f"Claude: batch {batch.id} submitted ({len(chunk)} posts). Waiting (usually under an hour)...")
        deadline = time.time() + 5 * 3600
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            if time.time() > deadline:
                log("  batch still running after 5 hours; its posts will be retried next run")
                break
            time.sleep(60)
        if b.processing_status != "ended":
            continue
        for res in client.messages.batches.results(batch.id):
            if res.result.type == "succeeded":
                results[res.custom_id] = tool_input(res.result.message)
    return results


# ---------------------------------------------------------------- 5. outputs
def event_start(ev):
    if not ev.get("date"):
        return None
    try:
        d = date.fromisoformat(ev["date"])
    except ValueError:
        return None
    if ev.get("start_time"):
        try:
            hh, mm = map(int, ev["start_time"].split(":"))
            return datetime(d.year, d.month, d.day, hh, mm, tzinfo=CT)
        except ValueError:
            pass
    return datetime(d.year, d.month, d.day, tzinfo=CT)


def ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 73:
        cut = 73
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def write_ics(path, events, name):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ut-org-events//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{name}", "X-WR-TIMEZONE:America/Chicago"]
    for ev in events:
        start = event_start(ev)
        if not start:
            continue
        summary = f"{ev['title'] or 'Event'} ({ev['org_name']})" + (" - free food" if ev.get("free_food") else "")
        desc = "\n".join(x for x in [
            f"Food: {ev['food_details']}" if ev.get("free_food") and ev.get("food_details") else None,
            "Members only" if ev.get("members_only") else None,
            ev.get("notes"), f"Original post: {ev['post_url']}"] if x)
        lines += ["BEGIN:VEVENT", f"UID:{ev['id']}@ut-org-events", f"DTSTAMP:{stamp}"]
        if ev.get("start_time"):
            s_utc = start.astimezone(timezone.utc)
            end = None
            if ev.get("end_time"):
                try:
                    hh, mm = map(int, ev["end_time"].split(":"))
                    end = start.replace(hour=hh, minute=mm)
                    if end <= start:
                        end = None
                except ValueError:
                    end = None
            end = (end or start + timedelta(hours=1)).astimezone(timezone.utc)
            lines += [f"DTSTART:{s_utc:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}"]
        lines += [f"SUMMARY:{ics_escape(summary)}", f"LOCATION:{ics_escape(ev.get('location'))}",
                  f"DESCRIPTION:{ics_escape(desc)}", f"URL:{ev['post_url']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(ics_fold(l) for l in lines) + "\r\n", encoding="utf-8")


def rebuild_outputs(events):
    today = now_ct().date()
    keep = {}
    for k, ev in events.items():
        st = event_start(ev)
        if st and st.date() < today - timedelta(days=KEEP_PAST_DAYS):
            continue
        if not st and triage.parse_ts(ev["posted_at"]) < datetime.now(timezone.utc) - timedelta(days=10):
            continue
        keep[k] = ev
    save_json(EVENTS_FILE, keep)
    public = sorted((ev for ev in keep.values() if ev.get("is_event")),
                    key=lambda e: (e.get("date") or "9999", e.get("start_time") or "99"))
    DOCS.mkdir(exist_ok=True)
    save_json(DOCS / "events.json", {"updated": now_ct().isoformat(timespec="minutes"), "events": public})
    write_ics(DOCS / "events.ics", public, "UT Org Events")
    write_ics(DOCS / "free-food.ics", [e for e in public if e.get("free_food")], "UT Free Food")
    return public


# ---------------------------------------------------------------- main
def main():
    orgs = triage.load_orgs()
    seen = load_json(SEEN_FILE, {})
    events = load_json(EVENTS_FILE, {})
    kind, runs = plan_runs()

    rows = []
    for handles, window, limit in runs:
        if handles:
            rows += scrape(handles, window, limit)
    checked = triage.update_rows(rows)

    new_posts = []
    for r in rows:
        sc = r.get("shortCode")
        if r.get("error") or not sc or sc in seen or not r.get("timestamp"):
            continue
        h = triage.handle_from(r)
        if h not in orgs:
            continue
        seen[sc] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_posts.append((sc, r, orgs[h]))
    new_posts.sort(key=lambda t: t[1]["timestamp"], reverse=True)
    if len(new_posts) > MAX_POSTS_PER_RUN:
        log(f"Capping at {MAX_POSTS_PER_RUN} newest posts (had {len(new_posts)}).")
        for sc, _, _ in new_posts[MAX_POSTS_PER_RUN:]:
            seen.pop(sc, None)
        new_posts = new_posts[:MAX_POSTS_PER_RUN]
    log(f"{len(new_posts)} new posts to read.")

    params = {}
    for sc, post, org in new_posts:
        params[sc] = build_message(post, org)
    results = extract(params)

    found = food = 0
    for sc, post, org in new_posts:
        res = results.get(sc)
        if res is None:            # failed: forget it so the next run retries
            seen.pop(sc, None)
            continue
        if not res.get("is_event"):
            continue
        found += 1
        food += bool(res.get("free_food"))
        events[sc] = {
            "id": sc, **res,
            "org_handle": org["handle"], "org_name": org.get("name") or org["handle"],
            "purpose": org.get("purpose", ""), "school": org.get("school", ""),
            "membership": org.get("membership", ""),
            "cohosts": [c.get("username") for c in post.get("coauthorProducers") or [] if c.get("username")],
            "post_url": post.get("url"), "posted_at": post["timestamp"],
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    seen = {k: v for k, v in seen.items() if triage.parse_ts(v) > cutoff}
    save_json(SEEN_FILE, seen)
    public = rebuild_outputs(events)
    status = triage.report(quiet=True)

    empty = sum(1 for r in rows if r.get("error"))
    est_apify = len(rows) * 0.0027
    lines = [
        f"## Run summary ({now_ct():%a %b %-d, %-I:%M %p} Central, {kind})",
        f"- Accounts checked: {checked}",
        f"- Apify rows: {len(rows)} ({len(rows) - empty} posts, {empty} empty/not found), about ${est_apify:.2f}",
        f"- New posts read by Claude: {len(results)} of {len(new_posts)}",
        f"- New events found: {found} ({food} with free food)",
        f"- Events now on the site: {len(public)} ({sum(1 for e in public if e.get('free_food'))} with free food)",
        f"- Accounts: {status['checked_every_run']} checked every run, {status['monthly']} monthly, "
        f"{status['not_checked']} not checked, {status['warnings']} warnings",
    ]
    log("\n".join(lines))
    if SUMMARY_FILE:
        with open(SUMMARY_FILE, "a") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    for key in ("APIFY_TOKEN", "ANTHROPIC_API_KEY"):
        if not os.getenv(key) and not (os.getenv("DRY_RUN_FILE") and os.getenv("FAKE_CLAUDE")):
            sys.exit(f"Missing {key}. Add it under Settings > Secrets and variables > Actions, spelled exactly like this.")
    main()
