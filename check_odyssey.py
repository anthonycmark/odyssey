from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

STATE_PATH = Path("state.json")
STATE_VERSION = 5
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
FRONTIER_DAYS = int(os.getenv("FRONTIER_DAYS", "14"))
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
            "checked_dates": [],
            "health_error": "",
        }


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_new_date_notification(new_dates: list[str], showings: dict[str, dict]) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    new_dates = sorted(new_dates)
    lines: list[str] = []
    click = CANONICAL_THEATRE_URL

    for date_string in new_dates:
        items = sorted(
            (item for item in showings.values() if item["date"] == date_string),
            key=lambda item: item["time"],
        )
        times = ", ".join(item["time"] for item in items)
        lines.append(f"{date_string} — {times or 'showtimes listed on AMC'}")
        if items and click == CANONICAL_THEATRE_URL:
            click = items[0]["url"]

    headers = {
        "Title": "NEW Odyssey 70mm DAY added",
        "Priority": "urgent",
        "Tags": "ticket",
        "Click": click,
    }
    message = (
        "AMC added Odyssey IMAX 70mm showings on a NEW FUTURE CALENDAR DATE at Universal CityWalk:\n"
        + "\n".join(lines)
        + "\n\nChanges to times, seats, ticket availability, or sold-out status on an existing date are ignored."
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


def parse_listing_page(html: str, show_date: date) -> tuple[dict[str, dict], int, bool]:
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


def fetch_listing(session: requests.Session, show_date: date) -> tuple[dict[str, dict], int, bool]:
    last_error = ""
    for base_url in THEATRE_URLS:
        url = f"{base_url}?date={show_date.isoformat()}&premium-offering=imax"
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            return parse_listing_page(r.text, show_date)
        except Exception as exc:
            last_error = f"{url}: {exc}"
    raise RuntimeError(last_error or "both AMC listing URLs failed")


def dates_from_legacy_state(state: dict) -> set[str]:
    dates = set(state.get("seen_dates") or [])
    legacy_showings = state.get("showings") or state.get("active") or state.get("seen") or {}
    for item in legacy_showings.values():
        if isinstance(item, dict) and item.get("date"):
            dates.add(str(item["date"]))
    return dates


def future_date_objects(values: set[str], today: date) -> list[date]:
    result: list[date] = []
    for value in values:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        if parsed > today:
            result.append(parsed)
    return result


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
    seen_dates = dates_from_legacy_state(state)
    checked_dates = set(state.get("checked_dates") or [])

    local_today = datetime.now(PACIFIC).date()
    health_errors: list[str] = []

    # One cheap control read ensures AMC is returning a real theatre page rather than an empty shell.
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

    migration_baseline = previous_version < STATE_VERSION or not initialized

    # On migration, establish a silent baseline across the current window plus a frontier
    # beyond the latest already-known date. A never-before-observed date can NEVER alert;
    # it must first have been observed empty, then later gain Odyssey 70mm showings.
    future_seen = future_date_objects(seen_dates, local_today)
    latest_seen = max(future_seen) if future_seen else local_today

    if migration_baseline:
        scan_end = max(
            local_today + timedelta(days=DAYS_AHEAD),
            latest_seen + timedelta(days=FRONTIER_DAYS),
        )
        dates_to_check = [
            local_today + timedelta(days=offset)
            for offset in range(1, (scan_end - local_today).days + 1)
        ]
    else:
        # Existing dates are never scanned for alert purposes again. We only recheck dates
        # that have NOT yet had Odyssey 70mm, including gaps and a 14-day frontier.
        future_seen = future_date_objects(seen_dates, local_today)
        latest_seen = max(future_seen) if future_seen else local_today
        scan_end = latest_seen + timedelta(days=FRONTIER_DAYS)
        dates_to_check = []
        d = local_today + timedelta(days=1)
        while d <= scan_end:
            if d.isoformat() not in seen_dates:
                dates_to_check.append(d)
            d += timedelta(days=1)

    found_showings: dict[str, dict] = {}
    new_dates: list[str] = []
    successful_checks = 0

    for d in dates_to_check:
        date_string = d.isoformat()
        was_checked_before = date_string in checked_dates
        try:
            parsed, generic_count, signal = fetch_listing(session, d)
            successful_checks += 1
            found_showings.update(parsed)

            if signal and not parsed:
                health_errors.append(
                    f"AMC shows Odyssey + IMAX 70mm + a time on {d}, but no showing ID was parsed"
                )

            if parsed:
                times = ", ".join(sorted(item["time"] for item in parsed.values()))
                print(
                    f"{d}: {len(parsed)} Odyssey IMAX 70mm showing(s); "
                    f"times=[{times}]; checked_before={was_checked_before}"
                )

                # This is the only possible phone-alert condition:
                # - future date
                # - never previously had Odyssey IMAX 70mm
                # - we had ALREADY checked this exact date on an earlier run and found none
                # Therefore a date merely entering the scan frontier cannot generate a false alert.
                if (
                    not migration_baseline
                    and was_checked_before
                    and date_string not in seen_dates
                ):
                    new_dates.append(date_string)

                seen_dates.add(date_string)

            checked_dates.add(date_string)

        except Exception as exc:
            health_errors.append(f"AMC listing failed for {d}: {exc}")

    health_error = " | ".join(sorted(set(health_errors)))
    if health_error:
        print(f"HEALTH WARNING (log only; no phone notification): {health_error}")

    # Never send anything during a migration/baseline run. Also suppress alerts if
    # the health checks are questionable rather than risk a bad notification.
    if new_dates and not health_error:
        send_new_date_notification(sorted(set(new_dates)), found_showings)
        print(f"Sent phone notification for {len(set(new_dates))} truly new future date(s).")
    elif new_dates:
        print(
            f"Detected {len(set(new_dates))} possible new date(s), but health is WARNING; "
            "no phone notification sent."
        )

    save_state({
        "version": STATE_VERSION,
        "initialized": True,
        "seen_dates": sorted(seen_dates),
        "checked_dates": sorted(checked_dates),
        "health_error": health_error,
    })

    if migration_baseline:
        print(
            f"V{STATE_VERSION} DATE-ONLY baseline complete: {len(seen_dates)} date(s) already known, "
            f"{len(checked_dates)} future date(s) observed; NO phone alert sent."
        )
    else:
        print(
            f"Checked {successful_checks} unseen/gap/frontier date(s); "
            f"{len(set(new_dates))} truly new future date(s); "
            f"health={'OK' if not health_error else 'WARNING'}."
        )

    return 0 if not health_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
