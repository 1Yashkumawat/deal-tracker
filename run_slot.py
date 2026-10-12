#!/usr/bin/env python3
"""GitHub Actions runner: wait for 21:00 IST, poll until 01:00 IST, then exit.
Env: TEST_MINUTES (>0 = bypass IST window, run that many minutes), STATUS_CHAT_ID (optional heartbeat)."""
import json, os, time, urllib.request
from datetime import datetime, timedelta
import poll_deals as pd

IST = pd.IST
HEARTBEAT_S = 600


def window(now):
    """Return (start, end) of the slot that 'now' belongs to or the next one today."""
    if now.hour < 1:
        start = (now - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    elif now.hour < 12:
        return None  # morning: scheduled run is too late (after 01:00), skip
    else:
        start = now.replace(hour=21, minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=4)


def tg(method, payload):
    tok = os.environ.get("DEALWALA_BOT_TOKEN", "")
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("result")
    except Exception as e:
        print("heartbeat", method, "failed", type(e).__name__, flush=True)
        return None


def count_posts_today():
    today = datetime.now(IST) - timedelta(hours=12)  # night slot spans midnight
    n = 0
    if os.path.exists(pd.POSTED):
        for line in open(pd.POSTED):
            try:
                r = json.loads(line)
                if r.get("exit") == 0 and datetime.fromisoformat(r["posted_at"]) >= today.replace(hour=12, minute=0, second=0, microsecond=0):
                    n += 1
            except Exception:
                pass
    return n


def main():
    test_min = float(os.environ.get("TEST_MINUTES") or 0)
    now = datetime.now(IST)
    if test_min > 0:
        start, end = now, now + timedelta(minutes=test_min)
        print(f"TEST mode: IST window bypassed, running {test_min} min", flush=True)
    else:
        w = window(now)
        if not w or now >= w[1]:
            print(now.strftime("%H:%M IST"), "outside slot (after 01:00), exiting", flush=True)
            return
        start, end = w
    if now < start:
        print("waiting until", start.strftime("%H:%M IST"), flush=True)
        time.sleep((start - now).total_seconds())
    print("START slot until", end.strftime("%Y-%m-%d %H:%M IST"),
          "autopost", os.environ.get("TRACK_AUTOPOST") == "1", flush=True)
    chat = os.environ.get("STATUS_CHAT_ID", "").strip()
    if not chat:
        print("heartbeat skipped: STATUS_CHAT_ID not set", flush=True)
    msg_id, last_hb = None, 0
    interval = int(os.environ.get("TRACK_INTERVAL", "10"))
    while datetime.now(IST) < end:
        t = datetime.now(IST)
        try:
            n = pd.run_once()
            print(t.strftime("%H:%M:%S"), "new", len(n), flush=True)
        except Exception as e:
            print(t.strftime("%H:%M:%S"), "error", type(e).__name__, e, flush=True)
        if chat and time.time() - last_hb >= HEARTBEAT_S:
            text = (f"Deal Tracker active • last poll {t.strftime('%H:%M')} IST • posts today {count_posts_today()}"
                    f" • queued {len(pd.load_pending())}" + (" • TEST" if test_min else ""))
            if msg_id:
                tg("editMessageText", {"chat_id": chat, "message_id": msg_id, "text": text})
            else:
                r = tg("sendMessage", {"chat_id": chat, "text": text, "disable_notification": True})
                msg_id = r and r.get("message_id")
            last_hb = time.time()
        time.sleep(max(0, min(interval, (end - datetime.now(IST)).total_seconds())))
    if chat and msg_id:
        tg("editMessageText", {"chat_id": chat, "message_id": msg_id,
                               "text": f"Deal Tracker stopped {datetime.now(IST).strftime('%H:%M')} IST • posts today {count_posts_today()}"})
    print("STOP slot", datetime.now(IST).strftime("%H:%M:%S IST"), flush=True)


if __name__ == "__main__":
    main()
