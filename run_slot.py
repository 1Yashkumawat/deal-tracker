#!/usr/bin/env python3
"""GitHub Actions runner: poll continuously for one ~6h cron window, then exit so state is cached.
Env: TEST_MINUTES (>0 = short run), STATUS_CHAT_ID (heartbeat + alerts), FORCE_ALERT (test an alert)."""
import json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
import poll_deals as pd

IST = pd.IST
HEARTBEAT_S = 600
ALERT_GAP_S = 1800
MAX_RUN_MIN = 352          # job timeout is 360
BOUNDARIES_UTC = (0, 6, 12, 18)  # cron '5 0,6,12,18 * * *'
ALERTS_FILE = os.path.join(pd.BASE, "alerts.json")
LIVE_FILE = os.path.join(pd.BASE, "last_live.json")   # last time a live run was alive (cached)
GAP_ALERT_S = 900
FAILED_SENT = set()       # ASINs already reported as finally failed (this run)
STATS = {"ok": 0, "src": 0, "fail": 0, "retried_ok": 0, "_": 0}
CHAT = os.environ.get("STATUS_CHAT_ID", "").strip()


def tg(method, payload):
    tok = os.environ.get("DEALWALA_BOT_TOKEN", "")
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("result")
    except Exception as e:
        print("tg", method, "failed", type(e).__name__, flush=True)
        return None


def alert(kind, text):
    """At most one alert per kind per 30 min (persisted across runs via the cache)."""
    try:
        last = json.load(open(ALERTS_FILE))
    except Exception:
        last = {}
    if time.time() - last.get(kind, 0) < ALERT_GAP_S:
        print("alert rate-limited", kind, flush=True)
        return
    msg = f"⚠️ Deal Tracker alert [{kind}] {datetime.now(IST).strftime('%d %b %H:%M')} IST\n{text}"
    print("ALERT", kind, text[:200], flush=True)
    if not CHAT:
        print("alert not sent: STATUS_CHAT_ID not set", flush=True)
        return
    if tg("sendMessage", {"chat_id": CHAT, "text": msg[:4000]}):
        last[kind] = time.time()
        json.dump(last, open(ALERTS_FILE, "w"))


def now_ist():
    return datetime.now(IST).strftime("%H:%M")


def notify(text, quiet=False):
    """Send a NEW short message to STATUS_CHAT_ID (never an edit)."""
    print("NOTIFY", text[:200].replace("\n", " | "), flush=True)
    if CHAT:
        tg("sendMessage", {"chat_id": CHAT, "text": text[:4000], "disable_notification": quiet})


def is_live():
    return os.environ.get("TRACK_AUTOPOST") == "1"


def mark_alive():
    if is_live():
        try:
            json.dump({"ts": time.time()}, open(LIVE_FILE, "w"))
        except Exception:
            pass


def check_gap(start):
    """At live-run start: alert if the previous live run stopped > 15 min ago."""
    try:
        prev = json.load(open(LIVE_FILE))["ts"]
    except Exception:
        return
    if start.timestamp() - prev > GAP_ALERT_S:
        a = datetime.fromtimestamp(prev, IST).strftime("%d %b %H:%M")
        notify(f"⏸️ Run missed/gap: tracker was not running from {a} to {start.strftime('%d %b %H:%M')} IST "
               f"({int((start.timestamp() - prev) / 60)} min).")


def deal_info(r):
    out = r.get("out", "")
    def g(k):
        m = re.search(rf"^{k} (.+)$", out, re.M)
        return m.group(1).strip() if m else ""
    title = g("title") or r.get("asin", "?")
    pct = ""
    m = re.search(r"pct=(\d+)", out)
    if m:
        pct = m.group(1)
    else:
        try:
            pr, mrp = float(g("price").replace(",", "")), float(g("mrp").replace(",", ""))
            pct = str(round((mrp - pr) / mrp * 100)) if mrp > 0 else ""
        except Exception:
            pass
    return title[:70], pct


def deals_msg(posted):
    lines = [f"• {t} ({p}% off)" if p else f"• {t}" for t, p in posted]
    head = "✅ Deal posted" if len(posted) == 1 else f"✅ {len(posted)} deals posted"
    return f"{head} {now_ist()} IST\n" + "\n".join(lines[:15]) + (f"\n…+{len(lines)-15} more" if len(lines) > 15 else "")


def failed_msg(r, why):
    if r.get("asin") in FAILED_SENT:
        return None
    FAILED_SENT.add(r.get("asin"))
    return f"❌ {why}: {r.get('asin')} at {now_ist()} IST (exit {r.get('exit')})"


def run_end(start, test_min):
    if test_min > 0:
        return start + timedelta(minutes=test_min)
    # stop just before the next cron boundary so the next run starts on time (no overlap)
    u = start.astimezone(timezone.utc)
    nxt = None
    for d in (0, 1):
        for h in BOUNDARIES_UTC:
            c = (u + timedelta(days=d)).replace(hour=h, minute=5, second=0, microsecond=0)
            if c > u + timedelta(minutes=30) and nxt is None:
                nxt = c
    return min(start + timedelta(minutes=MAX_RUN_MIN), nxt.astimezone(IST) - timedelta(minutes=2))


def posts_today():
    day = datetime.now(IST).date()
    n = 0
    if os.path.exists(pd.POSTED):
        for line in open(pd.POSTED):
            try:
                r = json.loads(line)
                if r.get("exit") == 0 and datetime.fromisoformat(r["posted_at"]).date() == day:
                    n += 1
            except Exception:
                pass
    return n


def main():
    test_min = float(os.environ.get("TEST_MINUTES") or 0)
    start = datetime.now(IST)
    end = run_end(start, test_min)
    print("START", start.strftime("%H:%M:%S IST"), "until", end.strftime("%Y-%m-%d %H:%M IST"),
          "autopost", os.environ.get("TRACK_AUTOPOST") == "1", "test", test_min, flush=True)
    if not CHAT:
        print("heartbeat/alerts disabled: STATUS_CHAT_ID not set", flush=True)
    tag = "" if is_live() else " • TEST (dry run, no posts)"
    if is_live():
        check_gap(start)
    notify(f"▶️ Deal Tracker run started {start.strftime('%H:%M')} IST • until {end.strftime('%H:%M')} IST{tag}", quiet=not is_live())
    mark_alive()
    if os.environ.get("FORCE_ALERT"):
        alert("test", "Test alert from workflow_dispatch (FORCE_ALERT). Alerts work.")
    nb = int(os.environ.get("TRACK_PROBE_BACKLOG") or 0)
    if nb and os.environ.get("TRACK_DRYRUN_PROBE"):  # dry-run measurement: probe the newest N page deals too
        recs = [{"key": pd.key_for(u), "post_time": p["time"], "title": p["title"], "src_price": p["src_price"]}
                for p in pd.parse(pd.fetch()) for u in dict.fromkeys(p["links"])]
        recs = [r for r in recs if r["key"].startswith("ASIN:")]
        print("backlog probe", len(recs[-nb:]), "of", len(recs), "page ASINs;",
              sum(1 for r in recs[-nb:] if r["src_price"]), "have explicit source price", flush=True)
        for r in recs[-nb:]:
            pd.probe_one(r["key"][5:], r)
    posted_seen = sum(1 for _ in open(pd.POSTED)) if os.path.exists(pd.POSTED) else 0
    msg_id, last_hb, fetch_fail_since, last_err, amz_fail_streak = None, 0, None, "", 0
    interval = int(os.environ.get("TRACK_INTERVAL", "10"))
    while datetime.now(IST) < end:
        t = datetime.now(IST)
        try:
            n = pd.run_once()
            fetch_fail_since = None
            print(t.strftime("%H:%M:%S"), "new", len(n), flush=True)
        except Exception as e:
            last_err = f"{t.strftime('%H:%M')} t.me fetch {type(e).__name__}"
            print(t.strftime("%H:%M:%S"), "error", type(e).__name__, e, flush=True)
            fetch_fail_since = fetch_fail_since or time.time()
            if time.time() - fetch_fail_since > 300:
                alert("tme_fetch", f"t.me/s/{pd.CHANNEL} fetch failing for {int((time.time()-fetch_fail_since)/60)} min: {type(e).__name__}")
        # inspect new poster results (posted.jsonl) + dry-run probes
        recs, posted_now = [], []
        if os.path.exists(pd.POSTED):
            lines = open(pd.POSTED).read().splitlines()
            recs, posted_seen = lines[posted_seen:], len(lines)
        for line in recs + pd.PROBE_RESULTS:
            r = json.loads(line) if isinstance(line, str) else line
            out = r.get("out", "")
            if r.get("requeued") or (r.get("exit") == 4 and not r.get("final")):
                continue  # captcha, will be retried with backoff
            no_price = ("no price" in out or "ERR amazon" in out or "no asin" in out or r.get("exit") in (3, 4))
            if r.get("probe"):
                STATS["ok" if r.get("exit") == 0 and "DRYRUN" in out else ("src" if "SOURCEPRICE" in out else "fail")] += 1
                STATS["retried_ok" if r.get("exit") == 0 and r.get("attempt") else "_"] += 1
            amz_fail_streak = amz_fail_streak + 1 if no_price else (0 if r.get("exit") in (0, 2) else amz_fail_streak)
            if amz_fail_streak >= 3:
                alert("amazon_fetch", f"Amazon price fetch failed for {amz_fail_streak} deals in a row (last {r.get('asin')}).")
            if r.get("exit") not in (0, 2) and not no_price and not r.get("probe"):
                last_err = f"{t.strftime('%H:%M')} post {r.get('asin')} exit {r.get('exit')}"
                alert("post_failed", f"Post failed for {r.get('asin')} (exit {r.get('exit')}):\n{out[-300:]}")
                m = failed_msg(r, "Upload failed")
                if m:
                    notify(m)
            elif no_price:
                last_err = f"{t.strftime('%H:%M')} no price {r.get('asin')}"
                if not r.get("probe"):
                    m = failed_msg(r, "Amazon blocked / no price, deal skipped")
                    if m:
                        notify(m)
            if r.get("exit") == 0 and not r.get("probe") and "post_id" in out:
                posted_now.append(deal_info(r))
        if posted_now:
            notify(deals_msg(posted_now))
        mark_alive()
        pd.PROBE_RESULTS.clear()
        if CHAT and time.time() - last_hb >= HEARTBEAT_S:
            text = (f"Deal Tracker active • last poll {t.strftime('%H:%M')} IST • posts today {posts_today()}"
                    f" • queued {len(pd.load_pending())}" + (f" • last error: {last_err}" if last_err else "")
                    + (" • TEST" if test_min else ""))
            if msg_id:
                tg("editMessageText", {"chat_id": CHAT, "message_id": msg_id, "text": text})
            else:
                r = tg("sendMessage", {"chat_id": CHAT, "text": text, "disable_notification": True})
                msg_id = r and r.get("message_id")
            last_hb = time.time()
        time.sleep(max(0, min(interval, (end - datetime.now(IST)).total_seconds())))
    mark_alive()
    notify(f"⏹️ Deal Tracker run ended {now_ist()} IST • posts today {posts_today()} • next run starts automatically{tag}",
           quiet=not is_live())
    if os.environ.get("TRACK_DRYRUN_PROBE"):
        t = STATS["ok"] + STATS["src"] + STATS["fail"]
        print(f"AMAZON SUMMARY amazon_ok={STATS['ok']} (after_retry={STATS['retried_ok']}) source_price_fallback={STATS['src']} failed={STATS['fail']} total={t}", flush=True)
    print("STOP", datetime.now(IST).strftime("%H:%M:%S IST"), flush=True)


if __name__ == "__main__":
    if "--alert" in sys.argv:  # used by the workflow's failure step
        alert(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        main()
