#!/usr/bin/env python3
"""
Verbier Opening Session ticket monitor -> Telegram.

The ticket page shows a banner "Today's ski pass quota has been sold out (d/m/yy)".
Staff seem to update it by hand AFTER a quota sells out, so the banner can still
show yesterday's date while today's tickets are on sale. Hence three states:

  SOLD     banner present with TODAY's date  -> today's quota is gone
  PENDING  banner present with an OLDER date -> today's quota not sold out yet:
           either not released yet, or ON SALE RIGHT NOW
  OPEN     no banner at all                  -> very likely on sale

Telegram messages:
  * at 00:01 (Zurich) every day: "new day" + current status of each page
  * every time a page's state changes: what it changed to
  * while a page is OPEN: a reminder every REPEAT_OPEN_EVERY seconds
  * on (re)start: one short heartbeat line
  * after ERROR_ALERT_AFTER failed checks in a row: an error alert

State is saved to state.json so a restart (GitHub Actions restarts the job every
~6 h) does not lose a change that happened in between.

Env vars:  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Usage:
  python monitor.py --test            # send a test message and exit
  python monitor.py --once            # one check, print status
  python monitor.py                   # loop forever (local computer)
  python monitor.py --duration 20700  # loop for N seconds (GitHub Actions)
"""
import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

PAGES = [
    {
        "key": "no_train",
        "name": "БЕЗ поезда",
        "url": "https://verbier4vallees.ch/en/online-shop/activities/opening-session_activity_410650",
    },
    {
        "key": "train",
        "name": "С поездом",
        "url": "https://verbier4vallees.ch/en/online-shop/activities/opening-session-st.nicholas-express_activity_426820",
    },
]

INTERVAL = 60             # seconds between checks (± jitter)
REPEAT_OPEN_EVERY = 600   # while OPEN, remind every N seconds
ERROR_ALERT_AFTER = 5     # consecutive failures before an error alert
DAY_START = (0, 1)        # (hour, minute) of the daily "new day" message
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TZ = ZoneInfo("Europe/Zurich")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SOLD_OUT_RE = re.compile(r"quota has been sold out\s*(?:\(\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*\))?", re.I)

LABEL = {
    "SOLD": "❌ на сегодня распродано",
    "PENDING": "⏳ сегодняшняя квота ещё не распродана: либо ещё не выложена, "
               "либо ПРОДАЁТСЯ ПРЯМО СЕЙЧАС — загляните на сайт",
    "OPEN": "🎟️ надписи «sold out» нет — похоже, билеты В ПРОДАЖЕ, покупайте!",
    "ERROR": "⚠️ не удалось проверить страницу",
}


def zurich_now():
    return datetime.now(TZ)


def stamp():
    return zurich_now().strftime("%d.%m %H:%M:%S")


def fetch(url):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}_={int(time.time())}", headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en,fr;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def page_status(raw_html, today):
    """Return (state, banner_date_str). Raises if the page looks wrong."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_html, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    if "Opening Session" not in text:
        raise ValueError("unexpected page content (blocked or redesigned?)")
    m = SOLD_OUT_RE.search(text)
    if not m:
        return "OPEN", None
    if not m.group(1):
        return "SOLD", "?"          # banner without a date: assume it's current
    d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    banner = date(y, mth, d)
    return ("SOLD" if banner >= today else "PENDING"), banner.strftime("%d.%m")


def telegram(msg):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    print(f"[{stamp()}] -> TG: {msg}")
    if not token or not chat:
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": msg, "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    except Exception as e:
        print(f"[{stamp()}] Telegram error: {e}", file=sys.stderr)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"day": None, "pages": {}}


def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        print(f"[{stamp()}] cannot save state: {e}", file=sys.stderr)


def line(p, ps):
    extra = f" (на баннере {ps['banner']})" if ps.get("banner") and ps["state"] != "OPEN" else ""
    return f"• {p['name']}: {LABEL.get(ps['state'], ps['state'])}{extra}"


def check_all(st, today):
    """Check every page; return list of (page, old_state, new_state)."""
    changes = []
    for p in PAGES:
        ps = st["pages"].setdefault(p["key"], {"state": None, "banner": None,
                                               "errors": 0, "last_alert": 0})
        old = ps["state"]
        try:
            state, banner = page_status(fetch(p["url"]), today)
            ps["errors"] = 0
        except Exception as e:
            ps["errors"] += 1
            print(f"[{stamp()}] {p['name']}: ERROR {e}")
            if ps["errors"] == ERROR_ALERT_AFTER:
                telegram(f"⚠️ {ERROR_ALERT_AFTER} раз подряд не удаётся открыть страницу "
                         f"«{p['name']}»: {e}\n{p['url']}")
            continue
        print(f"[{stamp()}] {p['name']}: {state} {banner or ''}")
        ps["state"], ps["banner"] = state, banner
        if state != old:
            changes.append((p, old, state))
    return changes


def run(duration=None, once=False):
    st = load_state()
    started = time.time()
    first_round = True
    while True:
        now = zurich_now()
        today = now.date()
        changes = check_all(st, today)

        new_day = (st["day"] != today.isoformat()
                   and (now.hour, now.minute) >= DAY_START)

        if new_day:
            st["day"] = today.isoformat()
            body = "\n".join(line(p, st["pages"][p["key"]]) for p in PAGES
                             if st["pages"].get(p["key"], {}).get("state"))
            telegram(f"🌅 Новый день ({today.strftime('%d.%m')}) — начинаю проверку.\n{body}")
        elif first_round and not once:
            body = "\n".join(line(p, st["pages"][p["key"]]) for p in PAGES
                             if st["pages"].get(p["key"], {}).get("state"))
            telegram(f"✅ Бот (пере)запущен, проверяю каждую минуту.\n{body}")
        else:
            for p, old, new in changes:
                ps = st["pages"][p["key"]]
                telegram(f"🔔 {p['name']}: статус изменился\n"
                         f"{LABEL.get(old, old or '—')}\n→ {LABEL[new]}\n{p['url']}")
                if new == "OPEN":
                    ps["last_alert"] = time.time()

        if new_day or first_round:
            for p in PAGES:  # status already reported above; start reminder timer now
                if st["pages"].get(p["key"], {}).get("state") == "OPEN":
                    st["pages"][p["key"]]["last_alert"] = time.time()

        # keep nagging while tickets look available
        for p in PAGES:
            ps = st["pages"].get(p["key"], {})
            if ps.get("state") == "OPEN" and time.time() - ps.get("last_alert", 0) >= REPEAT_OPEN_EVERY:
                if ps.get("last_alert"):
                    telegram(f"🎟️ Напоминание: {p['name']} — надписи «sold out» всё ещё нет!\n{p['url']}")
                ps["last_alert"] = time.time()

        save_state(st)
        first_round = False
        if once:
            return
        if duration and time.time() - started >= duration:
            print(f"[{stamp()}] duration reached, exiting")
            return
        time.sleep(max(10, INTERVAL + random.uniform(-10, 10)))


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
