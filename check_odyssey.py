from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
THEATRE_URL = "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-amc-at-citywalk-hollywood/showtimes"
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

SEAT_LINK_RE = re.compile(r"/showtimes/(\d+)/(?:seats|tickets)", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b", re.I)


def norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split()).lower()


def parse_event_showtimes(html: str, show_date: date) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}

    # AMC currently renders a distinct title for this special event. For each
    # ticket link, walk upward only through compact containers and require that
    # exact event title plus IMAX 70mm wording in the same container.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        match = SEAT_LINK_RE.search(href)
        if not match:
            continue

        event_text = None
        node = a
        for _ in range(8):
            node = getattr(node, "parent", None)
            if node is None:
                break
            text = " ".join(getattr(node, "stripped_strings", []))
            if len(text) > 3000:
                break
            low = norm(text)
            if "the odyssey – imax 70mm event" in low or "the odyssey - imax 70mm event" in low:
                if "imax 70mm" in low or ("imax" in low and "70mm" in low):
                    event_text = text
                    break

        if not event_text:
            continue

        sid = match.group(1)
        tm = TIME_RE.search(" ".join(a.stripped_strings)) or TIME_RE.search(event_text)
        display_time = tm.group(1).upper().replace(" ", "") if tm else "time listed on AMC"
        ticket_url = urljoin(THEATRE_URL, href.split("?")[0])
        key = f"{show_date.isoformat()}|{sid}"
        found[key] = {
            "date": show_date.isoformat(),
            "time": display_time,
            "showtime_id": sid,
            "url": ticket_url,
        }

    return found


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"initialized": False, "seen": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_notification(items: list[dict]) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    items = sorted(items, key=lambda x: (x["date"], x["time"]))
    lines = [f"{x['date']} — {x['time']}" for x in items[:10]]
    if len(items) > 10:
        lines.append(f"+ {len(items) - 10} more")

    message = "New Odyssey IMAX 70mm availability at Universal CityWalk:\n" + "\n".join(lines) + "\n\nTap to open AMC."
    headers = {
        "Title": "NEW Odyssey 70mm tickets",
        "Priority": "urgent",
        "Tags": "ticket",
        "Click": items[0]["url"],
    }
    response = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


def main() -> int:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })

    current: dict[str, dict] = {}
    successful_pages = 0
    today = date.today()

    for offset in range(DAYS_AHEAD + 1):
        d = today + timedelta(days=offset)
        url = f"{THEATRE_URL}?date={d.isoformat()}&premium-offering=imax"
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
            successful_pages += 1
            current.update(parse_event_showtimes(r.text, d))
        except Exception as exc:
            print(f"{d}: check failed: {exc}")

    if successful_pages == 0:
        print("No AMC pages could be checked; state left unchanged.")
        return 2

    state = load_state()
    initialized = bool(state.get("initialized", False))
    seen: dict[str, dict] = state.get("seen", {})

    if not initialized:
        save_state({"initialized": True, "seen": current})
        print(f"Baseline saved with {len(current)} matching purchasable showtime(s).")
        return 0

    new_items = [item for key, item in current.items() if key not in seen]
    if new_items:
        send_notification(new_items)
        print(f"Sent phone notification for {len(new_items)} new showtime(s).")

    seen.update(current)
    save_state({"initialized": True, "seen": seen})
    print(f"Checked {successful_pages} dates; {len(current)} matching showtime(s) visible; {len(new_items)} new.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
