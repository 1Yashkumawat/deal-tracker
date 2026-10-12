#!/usr/bin/env python3
"""Fast channel post. One page fetch, one photo card. No sticker. Never print tokens."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("DEALWALA_BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL_ID", "")
TAG = os.environ.get("AMAZON_TAG", "")
BLOCKED_ASINS = {"B0H9SB84R1", "B0H5VWCNWN", "B0DDHM6D3L", "B0DQPKH93F", "B0G261D7YJ"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def die(msg):
    print("ERR", msg)
    raise SystemExit(1)


def api(method, payload, timeout=30):
    if not TOKEN:
        die("poster token missing")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace").replace(TOKEN, "[redacted]")
        die(f"{method} HTTP {exc.code} {raw[:180]}")
    except Exception as exc:
        die(f"{method} {type(exc).__name__}")
    if not body.get("ok"):
        die(f"{method} not ok")
    return body["result"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def clean_title(html, final_url):
    m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)', html)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        t = re.sub(r"\s*:\s*Amazon\.in.*$", "", t)
        return t
    return "Amazon deal"


def asin_of(url, html):
    m = re.search(r"/dp/([A-Z0-9]{10})", url) or re.search(r"/dp/([A-Z0-9]{10})", html)
    return m.group(1) if m else None


def price_of(html):
    m = re.search(r'"priceAmount"\s*:\s*([0-9.]+)', html)
    if m:
        return money(m.group(1))
    m = re.search(r'"displayPrice"\s*:\s*"₹\s*([0-9,]+(?:\.[0-9]+)?)"', html)
    if m:
        return money(m.group(1))
    m = re.search(r'class="a-price-whole">([0-9,]+)', html)
    if m:
        return money(m.group(1))
    return None


def mrp_of(html):
    m = re.search(r'List Price:\s*<span class="a-text-strike">\s*₹\s*([0-9,]+(?:\.[0-9]+)?)', html)
    if m:
        return money(m.group(1))
    m = re.search(r'a-text-strike[^>]*>\s*₹\s*([0-9,]+(?:\.[0-9]+)?)', html)
    if m:
        return money(m.group(1))
    return None


def money(raw):
    n = float(str(raw).replace(",", ""))
    if n <= 0:
        return None
    if n.is_integer():
        return f"₹{int(n):,}"
    return f"₹{n:,.2f}"


def image_of(html):
    m = re.search(r'"hiRes":"(https:[^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'"large":"(https:[^"]+)"', html)
    return m.group(1) if m else None


def parse_rupees(s):
    if not s:
        return None
    try:
        return float(str(s).replace("₹", "").replace(",", "").strip())
    except Exception:
        return None


def discount_pct(price_s, mrp_s):
    p = parse_rupees(price_s)
    m = parse_rupees(mrp_s)
    if not p or not m or m <= 0 or p >= m:
        return None
    return round((m - p) / m * 100, 1)


def main():
    if len(sys.argv) < 2:
        die("need a url")
    hindi = ""
    coupon = ""
    args = sys.argv[2:]
    i = 0
    user_sent = False
    tracker = False
    allow_low = False
    while i < len(args):
        if args[i] == "--hindi" and i + 1 < len(args):
            hindi = args[i + 1].strip()
            i += 2
        elif args[i] == "--coupon" and i + 1 < len(args):
            coupon = args[i + 1].strip()
            i += 2
        elif args[i] == "--user-sent":
            user_sent = True
            i += 1
        elif args[i] == "--tracker-source":
            tracker = True
            i += 1
        elif args[i] == "--allow-low-discount":
            allow_low = True
            i += 1
        else:
            i += 1
    if not (user_sent or tracker):
        die("refusing to post: no user-sent link")
    dry = "--dry-run" in args
    if not dry:
        missing = [n for n, v in (("DEALWALA_BOT_TOKEN", TOKEN), ("CHANNEL_ID", CHANNEL), ("AMAZON_TAG", TAG)) if not v]
        if missing:
            die("missing env: " + ", ".join(missing))
    started = time.perf_counter()
    try:
        final, html = fetch(sys.argv[1])
    except urllib.error.HTTPError as exc:
        die(f"amazon HTTP {exc.code}")
    except Exception as exc:
        die(f"amazon fetch {type(exc).__name__}")
    fetched = time.perf_counter()
    asin = asin_of(final, html)
    if not asin:
        die("no asin")
    title = clean_title(html, final)
    price = price_of(html)
    mrp = mrp_of(html)
    image = image_of(html)
    if asin in BLOCKED_ASINS:
        die("blocked asin")
    if "--dry-run" in args:
        blocked = "captcha" in html.lower() or "robot check" in html.lower()
        print(f"DRYRUN asin={asin} price={price} mrp={mrp} pct={discount_pct(price, mrp)} captcha={blocked} len={len(html)}")
        raise SystemExit(0 if price else 3)
    if tracker and not price:
        die("tracker post skipped: no price on Amazon page")
    if dry:
        blocked = "captcha" in html.lower() or "robot check" in html.lower()
        print(f"DRYRUN asin={asin} price={price} mrp={mrp} pct={discount_pct(price, mrp)} captcha={blocked}")
        raise SystemExit(0 if price else 3)
    if mrp and price and mrp == price:
        mrp = None
    # Tracker only: high-value (>= MIN_DISCOUNT) posts instantly; low-value deferred unless --allow-low-discount
    min_disc = float(os.environ.get("TRACK_MIN_DISCOUNT", "40"))
    if tracker and not allow_low and not user_sent:
        pct = discount_pct(price, mrp)
        if pct is not None and pct < min_disc:
            print(f"defer low_discount {pct}% (need {min_disc}%)")
            print("price", price)
            print("mrp", mrp)
            print("asin", asin)
            raise SystemExit(2)
    link = f"https://www.amazon.in/dp/{asin}?tag={TAG}"
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now = datetime.now()
    hour = now.strftime("%I").lstrip("0") or "12"
    stamp = now.strftime(f"%d %b %Y, {hour}:%M %p IST")
    lines = []
    if hindi:
        lines.append(hindi)
    lines.append(title)
    if price:
        lines.append(f"🔥 डील: <b>{price}</b>")
    if coupon:
        lines.append(f"🎟 कूपन: {coupon}")
    if mrp:
        lines.append(f"💰 MRP: <b><s>{mrp}</s></b>")
    lines.append(f"⏰ समय: {stamp}")
    lines.append("")
    lines.append("Amazon Associate: क्वालिफाइंग परचेज़ से कमीशन")
    caption = "\n".join(lines)
    photo_payload = {
        "chat_id": int(CHANNEL) if CHANNEL.lstrip("-").isdigit() else CHANNEL,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "🛒 Buy Now", "url": link}]]},
    }
    if image:
        photo_payload["photo"] = image
        photo_res = api("sendPhoto", photo_payload)
    else:
        photo_payload["text"] = caption
        photo_res = api("sendMessage", {k: v for k, v in photo_payload.items() if k != "caption"})
    done = time.perf_counter()
    print("title", title[:120])
    print("price", price)
    print("mrp", mrp)
    print("asin", asin)
    print("post_id", photo_res.get("message_id"))
    print("fetch_s", round(fetched - started, 2))
    print("total_s", round(done - started, 2))


if __name__ == "__main__":
    main()
