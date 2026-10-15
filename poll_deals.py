#!/usr/bin/env python3
"""Poll the public preview of a Telegram channel (no admin, no bot token) and
record new Amazon links, deduplicated by ASIN (or normalized URL for non-product links).
With TRACK_AUTOPOST=1: high-discount (>= TRACK_MIN_DISCOUNT, default 40%) posts instantly;
low-discount ASINs wait TRACK_BATCH_DELAY_S (default 1200s = 20 min) then post.
Never posts anywhere without AUTOPOST. Output: new_links.jsonl, seen_*.json, pending_batch.json.
"""
import html, json, re, sys, time, urllib.request, os, subprocess
from datetime import datetime, timezone, timedelta

CHANNEL = os.environ.get("TRACK_CHANNEL", "deals")
BASE = os.environ.get("TRACK_STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(BASE, exist_ok=True)
POSTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deal_drop_post.py")
SEEN = f"{BASE}/seen_{CHANNEL}.json"
OUT = f"{BASE}/new_links.jsonl"
PENDING = f"{BASE}/pending_batch.json"
POSTED = f"{BASE}/posted.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_S = 900  # never post a backlog older than 15 min (e.g. after a restart)
MIN_DISCOUNT = float(os.environ.get("TRACK_MIN_DISCOUNT", "40"))
PROBE_RESULTS = []
RETRY = f"{BASE}/captcha_retry.json"
BATCH_DELAY_S = int(os.environ.get("TRACK_BATCH_DELAY_S", "1200"))  # 20 min mid of 15-30


def fetch():
    req = urllib.request.Request(f"https://t.me/s/{CHANNEL}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


AMZ_RE = re.compile(r"(amazon\.in|amzn\.(to|in)|amzn-to\.co)")
SHORT_RE = re.compile(r"(amzn\.(to|in)|amzn-to\.co)/")
KNOWN_HOSTS = ("t.me", "telegram.org", "telegram.me", "telegra.ph")
UNKNOWN_DOMAINS = set()
LAST_STATS = {"posts": 0, "amazon_links": 0}


def note_domain(u):
    """Log each non-Amazon link domain once per process (helps spot a new Amazon shortener)."""
    m = re.match(r"https?://([^/?#]+)", u)
    if not m:
        return
    h = m.group(1).lower()
    if h.startswith("www."):
        h = h[4:]
    if h.endswith(KNOWN_HOSTS) or h in UNKNOWN_DOMAINS:
        return
    UNKNOWN_DOMAINS.add(h)
    print("unknown link domain seen:", h, flush=True)


def resolve(u):
    """Follow redirects of an Amazon short link to the final amazon.in URL. Returns u on failure."""
    if not SHORT_RE.search(u):
        return u
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl() or u  # amazon may 503 the final page; the redirected URL is still known
    except Exception as e:
        print("resolve failed", re.sub(r"[?#].*", "", u), type(e).__name__, flush=True)
        return u


def parse(page):
    blocks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    posts = []
    for b in blocks:
        m = re.search(r'data-post="%s/(\d+)"' % re.escape(CHANNEL), b)
        if not m:
            continue
        pid = int(m.group(1))
        t = re.search(r'<time datetime="([^"]+)"', b)
        txt = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        text = ""
        if txt:
            text = re.sub(r"<br\s*/?>", "\n", txt.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        title = next((l.strip() for l in text.splitlines() if l.strip()), "")
        links = []
        for h in re.findall(r'href="([^"]+)"', b):
            u = html.unescape(html.unescape(h))
            if AMZ_RE.search(u):
                links.append(u)
            else:
                note_domain(u)
        posts.append({"post": pid, "time": t.group(1) if t else None, "title": title[:160], "links": links,
                      "src_price": source_price(text)})
    LAST_STATS["posts"] = len(posts)
    LAST_STATS["amazon_links"] = sum(len(x["links"]) for x in posts)
    return posts


_AMT = r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"


def source_price(text):
    """Explicit price/MRP stated in the source post, else None. Only used when TRACK_SOURCE_PRICE=1."""
    mrp = re.search(r"(?i)(?:M\.?R\.?P\.?|List price)\D{0,6}" + _AMT, text)
    amts = [(m.start(), m.group(1)) for m in re.finditer(_AMT, text)]
    price = next((a for s, a in amts if not mrp or a != mrp.group(1)), None)
    if not price:
        return None
    return {"price": price.replace(",", ""), "mrp": mrp.group(1).replace(",", "") if mrp else ""}


BACKOFF_S = [60, 120, 240, 360]  # ~13 min total, inside the 15-min freshness window


def key_for(u):
    a = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", u)
    if a:
        return "ASIN:" + a.group(1)
    return "URL:" + re.sub(r"[?&](tag|ref|btn_ref|qid|ds|dc|smid|psc|th)=[^&]*", "", u)


def load_pending():
    if not os.path.exists(PENDING):
        return []
    try:
        return json.load(open(PENDING))
    except Exception:
        return []


def save_pending(items):
    json.dump(items, open(PENDING, "w"), ensure_ascii=False, indent=0)


def queue_low(r, defer_note=""):
    items = load_pending()
    asin = r["key"][5:] if r["key"].startswith("ASIN:") else None
    if not asin:
        return
    if any(x.get("asin") == asin for x in items):
        return
    ready = datetime.now(IST) + timedelta(seconds=BATCH_DELAY_S)
    items.append({
        "asin": asin,
        "src_post": r["post"],
        "src_time": r["post_time"],
        "found_at": r["found_at"],
        "ready_at": ready.isoformat(timespec="seconds"),
        "note": defer_note,
        "title": r.get("title"),
        "src_price": r.get("src_price"),
    })
    save_pending(items)
    print("queued low_discount", asin, "ready", ready.strftime("%H:%M:%S"), defer_note, flush=True)


def poster_cmd(asin, r_meta, allow_low=False, dry=False, final=False):
    cmd = [sys.executable, POSTER, f"https://www.amazon.in/dp/{asin}", "--tracker-source"]
    if allow_low:
        cmd.append("--allow-low-discount")
    if dry:
        cmd.append("--dry-run")
    sp = r_meta.get("src_price")
    if sp:
        cmd += [f"--src-price={sp['price']}", f"--src-mrp={sp['mrp']}", f"--src-title={r_meta.get('title', '')}"]
    if final:
        cmd.append("--final-try")
    return cmd


def queue_retry(asin, r_meta, attempt, allow_low=False, dry=False):
    try:
        items = json.load(open(RETRY))
    except Exception:
        items = []
    if attempt >= len(BACKOFF_S) or any(x["asin"] == asin for x in items):
        return False
    due = time.time() + BACKOFF_S[attempt]
    items.append({"asin": asin, "meta": {k: r_meta.get(k) for k in ("post", "src_post", "post_time", "src_time", "found_at", "title", "src_price")},
                  "attempt": attempt + 1, "due": due, "allow_low": allow_low, "dry": dry})
    json.dump(items, open(RETRY, "w"), ensure_ascii=False)
    print("captcha requeue", asin, "attempt", attempt + 1, "in", BACKOFF_S[attempt], "s", flush=True)
    return True


def flush_retry():
    try:
        items = json.load(open(RETRY))
    except Exception:
        return
    now, keep, due = time.time(), [], []
    for it in items:
        (due if it["due"] <= now else keep).append(it)
    json.dump(keep, open(RETRY, "w"), ensure_ascii=False)
    for it in due:
        m = it["meta"]
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(m.get("src_time") or m.get("post_time"))).total_seconds()
        except Exception:
            age = 0
        if it["dry"]:
            age = 0  # dry-run probes measure fetch success only
        if age > (2700 if it["allow_low"] else MAX_AGE_S) or (it["dry"] and os.environ.get("TRACK_AUTOPOST") == "1"):
            print("drop stale captcha retry", it["asin"], "age", int(age), flush=True)
            continue
        final = it["attempt"] >= len(BACKOFF_S) or age > MAX_AGE_S - 120
        if it["dry"]:
            probe_one(it["asin"], it["meta"], it["attempt"], final)
        else:
            run_post(it["asin"], it["meta"], it["allow_low"], attempt=it["attempt"], final=final)


def run_post(asin, r_meta, allow_low=False, attempt=0, final=False):
    t0 = time.perf_counter()
    cmd = poster_cmd(asin, r_meta, allow_low, final=final)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        res = subprocess.CompletedProcess(cmd, 124, "", "poster timeout")
    out = {
        "asin": asin,
        "src_post": r_meta.get("src_post") or r_meta.get("post"),
        "src_time": r_meta.get("src_time") or r_meta.get("post_time"),
        "found_at": r_meta.get("found_at"),
        "posted_at": datetime.now(IST).isoformat(timespec="seconds"),
        "run_s": round(time.perf_counter() - t0, 2),
        "exit": res.returncode,
        "out": (res.stdout + res.stderr)[-400:],
        "allow_low": allow_low,
        "attempt": attempt,
        "final": final,
    }
    with open(POSTED, "a") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print("autopost", asin, "exit", res.returncode, ("low_batch" if allow_low else "instant"), flush=True)
    if res.returncode == 4 and not final:
        queue_retry(asin, r_meta, attempt, allow_low)
    return res


def flush_pending():
    items = load_pending()
    if not items:
        return
    now = datetime.now(IST)
    keep = []
    for it in items:
        try:
            ready = datetime.fromisoformat(it["ready_at"])
            if ready.tzinfo is None:
                ready = ready.replace(tzinfo=IST)
        except Exception:
            ready = now
        if ready > now:
            keep.append(it)
            continue
        # Age guard from original source post time
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(it["src_time"])).total_seconds()
        except Exception:
            age = 0
        # After batch delay, allow post even if original 15min window passed — but cap at 45 min total
        if age > 2700:
            print("drop stale low_batch", it["asin"], "age", int(age), flush=True)
            continue
        run_post(it["asin"], it, allow_low=True)
    save_pending(keep)


def autopost(r):
    if not r["key"].startswith("ASIN:"):
        return
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(r["post_time"])).total_seconds()
    except Exception:
        age = 0
    if age > MAX_AGE_S:
        return
    asin = r["key"][5:]
    res = run_post(asin, r, allow_low=False)
    if res.returncode == 2:
        note = ""
        for line in (res.stdout or "").splitlines():
            if line.startswith("defer "):
                note = line
                break
        queue_low(r, note)


def probe_one(a, meta, attempt=0, final=False):
    res = subprocess.run(poster_cmd(a, meta, dry=True, final=final), capture_output=True, text=True, timeout=120)
    last = (res.stdout + res.stderr).strip().splitlines()
    print("probe", a, "attempt", attempt, "exit", res.returncode, last[-1] if last else "", flush=True)
    if res.returncode == 4 and not final and queue_retry(a, meta, attempt, dry=True):
        PROBE_RESULTS.append({"asin": a, "exit": 4, "out": "requeued", "probe": True, "requeued": True})
        return
    PROBE_RESULTS.append({"asin": a, "exit": res.returncode, "out": last[-1] if last else "", "probe": True,
                          "attempt": attempt, "src_price": bool(meta.get("src_price"))})


def probe(new):
    """Dry run: fetch Amazon pages for new ASINs and report price parse, never posts."""
    limit = int(os.environ.get("TRACK_DRYRUN_PROBE", "0"))
    for r in [r for r in new if r["key"].startswith("ASIN:")][-limit:]:
        probe_one(r["key"][5:], r)


def run_once():
    seen = {}
    if os.path.exists(SEEN):
        seen = json.load(open(SEEN))
    new = []
    for p in parse(fetch()):
        for u in dict.fromkeys(p["links"]):
            k = key_for(u)
            if k in seen:
                continue
            if SHORT_RE.search(u):  # short link: resolve to the real amazon.in URL / ASIN
                seen[k] = {"post": p["post"], "first_seen": datetime.now(IST).isoformat(timespec="seconds")}
                u = resolve(u)
                k = key_for(u)
                if k in seen:
                    continue
            seen[k] = {"post": p["post"], "first_seen": datetime.now(IST).isoformat(timespec="seconds")}
            rec = {"channel": CHANNEL, "post": p["post"], "post_time": p["time"], "title": p["title"], "src_price": p["src_price"],
                   "key": k, "url": u, "found_at": seen[k]["first_seen"]}
            new.append(rec)
    if new:
        with open(OUT, "a") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    json.dump(seen, open(SEEN, "w"))
    if os.environ.get("TRACK_DRYRUN_PROBE"):
        flush_retry()
        probe(new)
    if os.environ.get("TRACK_AUTOPOST") == "1":
        flush_retry()
        flush_pending()
        for r in new:
            autopost(r)
        flush_pending()
    return new


if __name__ == "__main__":
    if "--loop" in sys.argv:
        interval = int(os.environ.get("TRACK_INTERVAL", "60"))
        while True:
            try:
                n = run_once()
                print(datetime.now(IST).strftime("%H:%M:%S"), "new", len(n), flush=True)
            except Exception as e:
                print(datetime.now(IST).strftime("%H:%M:%S"), "error", type(e).__name__, e, flush=True)
            time.sleep(interval)
    elif "--flush-pending" in sys.argv:
        flush_pending()
    else:
        for r in run_once():
            print(json.dumps(r, ensure_ascii=False))
