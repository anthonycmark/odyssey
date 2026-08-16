from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
STATE_VERSION = 4
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

THEATRE_URLS = [
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-amc-at-citywalk-hollywood/showtimes",
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-an-amc-theatre/showtimes",
]
CANONICAL_THEATRE_URL = THEATRE_URLS[0]

SHOWTIME_LINK_RE = re.compile(
    r"/showtimes/(\d+)(?:(?:/(?:seats|tickets))?(?:[?#]|$))",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b", re.I)


def norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split()).lower()


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "version": 0,
            "initialized": False,
            "seen_dates": [],
            "showings": {},
            "health_error": "",
        }


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_new_date_notification(new_dates: list[str], showings: dict[str, dict]) -> None:
    """Phone alerts are ONLY for calendar dates that have never been seen before."""
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    new_dates = sorted(new_dates)
    lines: list[str] = []
    click = CANONICAL_THEATRE_URL

    for date_string in new_dates:
        date_items = sorted(
            (item for item in showings.values() if item["date"] == date_string),
            key=lambda item: item["time"],
        )
        times = ", ".join(item["time"] for item in date_items)
        lines.append(f"{date_string} — {times or 'showtimes listed on AMC'}")
        if date_items and click == CANONICAL_THEATRE_URL:
            click = date_items[0]["url"]

    headers = {
        "Title": "NEW Odyssey 70mm date added",
        "Priority": "urgent",
        "Tags": "ticket",
        "Click": click,
    }
    message = (
        "AMC added Odyssey IMAX 70mm showings on a NEW FUTURE DATE at Universal CityWalk:\n"
        + "\n".join(lines)
        + "\n\nNo alerts are sent for new times or ticket changes on dates already seen."
    )
    response = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


def local_showtime_text(a) -> str:
    best = " ".join(a.stripped_strings)
    node = a
    for _ in range(4):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(getattr(node, "stripped_strings", []))
        if len(text) > 1200:
            break
        if TIME_RE.search(text):
            best = text
    return best


def nearby_odyssey_70mm_text(a) -> str | None:
    node = a
    for _ in range(12):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(getattr(node, "stripped_strings", []))
        if len(text) > 7000:
            break
        low = norm(text)
        if "the odyssey" in low and "imax" in low and ("70mm" in low or "70 mm" in low):
            return text
    return None


def page_has_odyssey_70mm_with_time(soup: BeautifulSoup) -> bool:
    for text_node in soup.find_all(string=re.compile(r"the odyssey", re.I)):
        node = getattr(text_node, "parent", None)
        for _ in range(8):
            if node is None:
                break
            text = " ".join(getattr(node, "stripped_strings", []))
            if len(text) > 6000:
                break
            low = norm(text)
            if (
                "the odyssey" in low
                and "imax" in low
                and ("70mm" in low or "70 mm" in low)
                and TIME_RE.search(text)
            ):
                return True
            node = getattr(node, "parent", None)
    return False


def parse_listing_page(html: str, show_date) -> tuple[dict[str, dict], int, bool]:
    """Return every listed Odyssey IMAX 70mm showing; ticket status does not affect date tracking."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    generic_ids: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        match = SHOWTIME_LINK_RE.search(href)
        if not match:
            continue

        sid = match.group(1)
        generic_ids.add(sid)

        if not nearby_odyssey_70mm_text(a):
            continue

        one_showtime_text = local_showtime_text(a)
        tm = TIME_RE.search(" ".join(a.stripped_strings)) or TIME_RE.search(one_showtime_text)
        display_time = tm.group(1).upper().replace(" ", "") if tm else "time listed on AMC"
        showing_url = urljoin(CANONICAL_THEATRE_URL, href.split("?")[0])
        key = f"{show_date.isoformat()}|{sid}"
        found[key] = {
            "date": show_date.isoformat(),
            "time": display_time,
            "showtime_id": sid,
            "url": showing_url,
            "sold_out": "sold out" in norm(one_showtime_text),
        }

    return found, len(generic_ids), page_has_odyssey_70mm_with_time(soup)


def fetch_listing(session: requests.Session, show_date) -> tuple[dict[str, dict], int, bool, str]:
    last_error = ""
    for base_url in THEATRE_URLS:
        url = f"{base_url}?date={show_date.isoformat()}&premium-offering=imax"
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            parsed, generic_count, signal = parse_listing_page(r.text, show_date)
            return parsed, generic_count, signal, url
        except Exception as exc:
            last_error = f"{url}: {exc}"
    raise RuntimeError(last_error or "both AMC listing URLs failed")


def legacy_dates_from_state(state: dict) -> set[str]:
    """Preserve every date remembered by older state formats during migration."""
    dates = set(state.get("seen_dates") or [])
    legacy_showings = state.get("showings") or state.get("active") or state.get("seen") or {}
    for item in legacy_showings.values():
        if isinstance(item, dict) and item.get("date"):
            dates.add(str(item["date"]))
    return dates


def main() -> int:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })

    state = load_state()
    previous_version = int(state.get("version", 0) or 0)
    initialized = bool(state.get("initialized", False))
    seen_dates = legacy_dates_from_state(state)

    local_today = datetime.now(PACIFIC).date()
    today_string = local_today.isoformat()
    health_errors: list[str] = []

    control_date = local_today + timedelta(days=1)
    control_url = f"{CANONICAL_THEATRE_URL}?date={control_date.isoformat()}"
    try:
        r = session.get(control_url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        control_text = norm(" ".join(soup.stripped_strings))
        control_ids = {
            m.group(1)
            for a in soup.find_all("a", href=True)
            if (m := SHOWTIME_LINK_RE.search(a.get("href", "")))
        }
        print(
            f"Health control {control_date}: HTTP {r.status_code}, "
            f"{len(r.text)} bytes, {len(control_ids)} generic showtime link(s)."
        )
        if "universal cinema" not in control_text:
            health_errors.append("AMC control page no longer identifies Universal Cinema")
        if len(control_ids) < 5:
            health_errors.append(
                f"AMC control page exposed only {len(control_ids)} generic showtime links; parsing may be broken"
            )
    except Exception as exc:
        health_errors.append(f"AMC control page request failed: {exc}")

    current_showings: dict[str, dict] = {}
    successful_dates = 0

    for offset in range(DAYS_AHEAD + 1):
        d = local_today + timedelta(days=offset)
        try:
            parsed, generic_count, signal, source_url = fetch_listing(session, d)
            successful_dates += 1
            current_showings.update(parsed)

            if signal and not parsed:
                health_errors.append(
                    f"AMC shows Odyssey + IMAX 70mm + a time on {d}, but no showing ID was parsed"
                )

            if parsed or signal:
                times = ", ".join(sorted(item["time"] for item in parsed.values()))
                print(
                    f"{d}: {len(parsed)} listed Odyssey IMAX 70mm showing(s), "
                    f"{generic_count} total IMAX link(s), signal={signal}; times=[{times}]"
                )
        except Exception as exc:
            health_errors.append(f"AMC listing failed for {d}: {exc}")

    if successful_dates == 0:
        health_errors.append("No AMC dates could be checked")

    health_error = " | ".join(sorted(set(health_errors)))
    if health_error:
        print(f"HEALTH WARNING (log only; no phone notification): {health_error}")

    current_dates = {item["date"] for item in current_showings.values()}

    # V4 changes the alert unit from individual showtimes to CALENDAR DATES.
    # Baseline everything currently visible without notifying. Older known dates
    # are retained permanently so a disappearing/reappearing date cannot alert.
    migration_baseline = previous_version < STATE_VERSION
    if migration_baseline or not initialized:
        seen_dates.update(current_dates)
        save_state({
            "version": STATE_VERSION,
            "initialized": True,
            "seen_dates": sorted(seen_dates),
            "showings": current_showings,
            "health_error": health_error,
        })
        print(
            f"V{STATE_VERSION} date baseline saved with {len(seen_dates)} known date(s); "
            "no phone alert sent for any existing date or time."
        )
        return 0 if not health_error else 2

    # EXACT ALERT RULE:
    # 1. The calendar date must never have been seen before.
    # 2. It must be strictly AFTER today in Pacific time.
    # New times, ticket restocks, sold-out/full changes, or reappearing showtimes
    # on a known date are ignored completely.
    new_future_dates = sorted(
        date_string
        for date_string in current_dates
        if date_string > today_string and date_string not in seen_dates
    )

    if new_future_dates and not health_error:
        send_new_date_notification(new_future_dates, current_showings)
        print(f"Sent phone notification for {len(new_future_dates)} brand-new future date(s).")
    elif new_future_dates:
        print(
            f"Detected {len(new_future_dates)} possible new future date(s), but health is WARNING; "
            "no phone notification sent."
        )

    # Once AMC has shown a date, remember it permanently. This prevents any
    # later changes within that date from ever generating another phone alert.
    seen_dates.update(current_dates)

    save_state({
        "version": STATE_VERSION,
        "initialized": True,
        "seen_dates": sorted(seen_dates),
        "showings": current_showings,
        "health_error": health_error,
    })

    print(
        f"Checked {successful_dates} dates; {len(current_dates)} date(s) currently listed; "
        f"{len(new_future_dates)} brand-new future date(s); "
        f"health={'OK' if not health_error else 'WARNING'}."
    )
    return 0 if not health_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
