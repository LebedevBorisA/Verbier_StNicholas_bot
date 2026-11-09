#!/usr/bin/env python3
"""
Verbier Opening Session ticket monitor -> Telegram.

Checks the ticket pages every few minutes. The page shows
"Today's ski pass quota has been sold out (dd/mm/yy)" while nothing is on sale;
when that line disappears (or changes), a Telegram message is sent.

Env vars:  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Usage:
  python monitor.py --test            # send a test message and exit
  python monitor.py --once            # one check, print status
  python monitor.py                   # loop forever (local computer)
  python monitor.py --duration 20700  # loop for N seconds (GitHub Actions)
"""
import argparse
import html
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

PAGES = [
    {
        "name": "БЕЗ поезда",
        "url": "https://verbier4vallees.ch/en/online-shop/activities/opening-session_activity_410650",
        "notify": True,
    },
    {
        "name": "С поездом (St. Nicholas Express)",
        "url": "https://verbier4vallees.ch/en/online-shop/activities/opening-session-st.nicholas-express_activity_426820",
        "notify": True,  # set to False if you don't want alerts for the train version
    },
]

INTERVAL = 120            # seconds between checks (± random jitter)
REPEAT_OPEN_EVERY = 600   # while tickets look available, re-alert every N seconds
ERROR_ALERT_AFTER = 5     # consecutive failures before an error alert
TZ = ZoneInfo("Europe/Zurich")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SOLD_OUT_RE = re.compile(r"quota has been sold out\s*(?:\(([^)]*)\))?", re.I)


def now():
    return datetime.now(TZ).strftime("%d.%m %H:%M:%S")


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en,fr;q=0.8",
        "Cache-Control": "no-cache",
    })
    # cache-buster so we never get a stale CDN copy
    sep = "&" if "?" in url else "?"
    req.full_url = f"{url}{sep}_={int(time.time())}"
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def page_status(raw_html):
    """Return ('soldout', '9/11/26') | ('open', None). Raises if page looks wrong."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_html, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    if "Opening Session" not in text:
        raise ValueError("unexpected page content (blocked or redesigned?)")
    m = SOLD_OUT_RE.search(text)
    if m:
        return "soldout", (m.group(1) or "").strip()
    return "open", None


def telegram(msg):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"[{now()}] (no Telegram config) {msg}")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": msg, "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    except Exception as e:
        print(f"[{now()}] Telegram error: {e}", file=sys.stderr)


def run(duration=None, once=False):
    state = {p["url"]: {"status": None, "date": None, "last_alert": 0, "errors": 0}
             for p in PAGES}
    started = time.time()
    while True:
        for p in PAGES:
            s = state[p["url"]]
            try:
                status, date = page_status(fetch(p["url"]))
                s["errors"] = 0
            except Exception as e:
                s["errors"] += 1
                print(f"[{now()}] {p['name']}: ERROR {e}")
                if s["errors"] == ERROR_ALERT_AFTER:
                    telegram(f"⚠️ Бот не может проверить страницу «{p['name']}» "
                             f"уже {ERROR_ALERT_AFTER} раз подряд: {e}\n{p['url']}")
                continue

            print(f"[{now()}] {p['name']}: {status} {date or ''}")
            first = s["status"] is None

            if status == "open":
                if p["notify"] and (s["status"] != "open"
                                    or time.time() - s["last_alert"] >= REPEAT_OPEN_EVERY):
                    telegram(f"🎟️ ПОХОЖЕ, БИЛЕТЫ В ПРОДАЖЕ — {p['name']}!\n"
                             f"Надпись «sold out» пропала ({now()}).\n{p['url']}")
                    s["last_alert"] = time.time()
            elif not first and p["notify"]:
                if s["status"] == "open":
                    telegram(f"❌ Снова sold out — {p['name']} ({date}).")
                elif date != s["date"]:
                    telegram(f"ℹ️ {p['name']}: надпись сменилась на «sold out ({date})» — "
                             f"квоту выложили и раскупили между проверками.")
            s["status"], s["date"] = status, date

        if once:
            return
        if duration and time.time() - started >= duration:
            print(f"[{now()}] duration reached, exiting")
            return
        time.sleep(INTERVAL + random.uniform(-20, 20))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--once", action="store_true", help="single check")
    ap.add_argument("--duration", type=int, help="stop after N seconds")
    a = ap.parse_args()
    if a.test:
        telegram("✅ Бот для билетов Opening Session подключён.")
    else:
        run(duration=a.duration, once=a.once)
