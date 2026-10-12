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


STATE_DIR = os.environ.get("TRACK_STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
COOKIES = os.path.join(STATE_DIR, "amazon_cookies.txt")
LAST_FETCH = os.path.join(STATE_DIR, "amazon_last_fetch")
MOBILE_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8,hi;q=0.6",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}


class Captcha(Exception):
    pass


def _opener():
    import http.cookiejar
    os.makedirs(STATE_DIR, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(COOKIES)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    return jar, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _space():
    """Keep >= 3-6 s (jittered) between Amazon requests across processes."""
    import random
    gap = random.uniform(3, 6)
    try:
        last = float(open(LAST_FETCH).read())
    except Exception:
        last = 0
    wait = last + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    open(LAST_FETCH, "w").write(str(time.time()))


def is_captcha(html):
    h = html.lower()
    return ("captcha" in h and "productTitle".lower() not in h) or "api-services-support@amazon.com" in h or len(html) < 8000


def _get(opener, url, ua, referer=None):
    _space()
    h = dict(HEADERS, **{"User-Agent": ua})
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
    with opener.open(urllib.request.Request(url, headers=h), timeout=20) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def fetch(url):
    """Shared cookie session, warm-up, jittered spacing, retry + mobile fallback. Raises Captcha."""
    jar, opener = _opener()
    if not any(c.domain.endswith("amazon.in") for c in jar):
        try:
            _get(opener, "https://www.amazon.in/", UA)
        except Exception:
            pass
    final, html = None, ""
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    tries = [(url, UA), (url, UA)]
    if m:
        tries.append((f"https://www.amazon.in/gp/aw/d/{m.group(1)}", MOBILE_UA))
    for i, (u, ua) in enumerate(tries):
        if i:
            time.sleep(4 + 3 * i)
        try:
            final, html = _get(opener, u, ua, referer="https://www.amazon.in/")
        except urllib.error.HTTPError as exc:
            if exc.code not in (503, 429):
                raise
            html = ""
        if html and not is_captcha(html):
            break
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    if not html or is_captcha(html):
        raise Captcha()
    return final, html


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


def source_post(url, args, tracker, allow_low, user_sent):
    """FLAGGED fallback (TRACK_SOURCE_PRICE=1): Amazon blocked, use price/MRP stated in the t.me source post."""
    val = lambda k: next((a.split("=", 1)[1] for a in args if a.startswith(k)), "")
    asin = re.search(r"/dp/([A-Z0-9]{10})", url).group(1)
    price, mrp, title = money(val("--src-price=")), money(val("--src-mrp=")) if val("--src-mrp=") else None, val("--src-title=") or "Amazon deal"
    pct = discount_pct(price, mrp)
    print(f"SOURCEPRICE asin={asin} price={price} mrp={mrp} pct={pct}")
    if "--dry-run" in args:
        raise SystemExit(0)
    if asin in BLOCKED_ASINS:
        die("blocked asin")
    if tracker and not allow_low and not user_sent and (pct is None or pct < float(os.environ.get("TRACK_MIN_DISCOUNT", "40"))):
        print(f"defer low_discount {pct}%")
        raise SystemExit(2)
    link = f"https://www.amazon.in/dp/{asin}?tag={TAG}"
    lines = [title, f"🔥 डील: <b>{price}</b>"] + ([f"💰 MRP: <b><s>{mrp}</s></b>"] if mrp else [])
    lines += [f"⏰ समय: {stamp_now()}", "", "Amazon Associate: क्वालिफाइंग परचेज़ से कमीशन"]
    res = api("sendMessage", {"chat_id": chat_id(), "text": "\n".join(lines), "parse_mode": "HTML",
                              "reply_markup": {"inline_keyboard": [[{"text": "🛒 Buy Now", "url": link}]]}})
    print("asin", asin, "post_id", res.get("message_id"), "source_price")


def chat_id():
    return int(CHANNEL) if CHANNEL.lstrip("-").isdigit() else CHANNEL


def stamp_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    hour = now.strftime("%I").lstrip("0") or "12"
    return now.strftime(f"%d %b %Y, {hour}:%M %p IST")


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
    except Captcha:
        src = [a for a in args if a.startswith("--src-price=")]
        if not (src and os.environ.get("TRACK_SOURCE_PRICE") == "1" and "--final-try" in args):
            print("captcha robot-check page (retry later)")
            raise SystemExit(4)
        return source_post(sys.argv[1], args, tracker, allow_low, user_sent)
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
