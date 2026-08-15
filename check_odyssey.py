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
STATE_VERSION = 2
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

THEATRE_URLS = [
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-amc-at-citywalk-hollywood/showtimes",
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-an-amc-theatre/showtimes",
]
CANONICAL_THEATRE_URL = THEATRE_URLS[0]

# AMC currently uses all of these forms in its purchase flow:
# /showtimes/<id>, /showtimes/<id>/seats, /showtimes/<id>/tickets.
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
        return {"version": 0, "initialized": False, "active": {}, "health_error": ""}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_text_notification(title: str, message: str, priority: str = "high", click: str | None = None) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    headers = {"Title": title, "Priority": priority, "Tags": "ticket"}
    if click:
        headers["Click"] = click

    response = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


def send_ticket_notification(items: list[dict]) -> None:
    items = sorted(items, key=lambda x: (x["date"], x["time"]))
    lines = [f"{x['date']} — {x['time']}" for x in items[:12]]
    if len(items) > 12:
        lines.append(f"+ {len(items) - 12} more")

    send_text_notification(
        "NEW Odyssey 70mm tickets",
        "New Odyssey IMAX 70mm ticket links appeared at Universal CityWalk:\n"
        + "\n".join(lines)
        + "\n\nTap to open AMC.",
        priority="urgent",
        click=items[0]["url"],
    )


def local_showtime_text(a) -> str:
    """Smallest nearby text block that appears to describe this one showtime."""
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
    """Tie a clickable time to a nearby Odyssey + IMAX + 70mm movie/format block."""
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
    """Independent signal used to detect a parser break instead of silently missing tickets."""
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

        movie_format_text = nearby_odyssey_70mm_text(a)
        if not movie_format_text:
            continue

        one_showtime_text = local_showtime_text(a)
        # A Sold Out showtime should not count as purchasable even if AMC leaves a link.
        if "sold out" in norm(one_showtime_text):
            continue

        tm = TIME_RE.search(" ".join(a.stripped_strings)) or TIME_RE.search(one_showtime_text)
        display_time = tm.group(1).upper().replace(" ", "") if tm else "time listed on AMC"
        ticket_url = urljoin(CANONICAL_THEATRE_URL, href.split("?")[0])
        key = f"{show_date.isoformat()}|{sid}"
        found[key] = {
            "date": show_date.isoformat(),
            "time": display_time,
            "showtime_id": sid,
            "url": ticket_url,
        }

    return found, len(generic_ids), page_has_odyssey_70mm_with_time(soup)


def fetch_listing(session: requests.Session, show_date) -> tuple[dict[str, dict], int, bool, str]:
    """Use the canonical CityWalk URL, falling back to AMC's alternate slug on failure."""
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
    previous_active: dict[str, dict] = state.get("active") or state.get("seen") or {}
    previous_health_error = state.get("health_error", "")

    local_today = datetime.now(PACIFIC).date()
    health_errors: list[str] = []

    # CONTROL CHECK: tomorrow's unfiltered CityWalk page should have many normal
    # clickable showtimes. This proves AMC isn't just returning an empty shell.
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

    current: dict[str, dict] = {}
    successful_dates = 0

    for offset in range(DAYS_AHEAD + 1):
        d = local_today + timedelta(days=offset)
        try:
            parsed, generic_count, signal, source_url = fetch_listing(session, d)
            successful_dates += 1
            current.update(parsed)

            if signal and not parsed:
                health_errors.append(
                    f"AMC shows Odyssey + IMAX 70mm + a time on {d}, but no clickable showtime link was parsed"
                )

            if parsed or signal:
                times = ", ".join(sorted(item["time"] for item in parsed.values()))
                print(
                    f"{d}: {len(parsed)} clickable Odyssey IMAX 70mm link(s), "
                    f"{generic_count} total IMAX link(s), signal={signal}; times=[{times}]"
                )
        except Exception as exc:
            health_errors.append(f"AMC listing failed for {d}: {exc}")

    if successful_dates == 0:
        health_errors.append("No AMC dates could be checked")

    health_error = " | ".join(sorted(set(health_errors)))

    if health_error and health_error != previous_health_error:
        send_text_notification(
            "Odyssey bot health warning",
            "The monitor detected a possible AMC parsing/fetch problem:\n\n"
            + health_error
            + "\n\nDo not rely on silent checks until this is fixed.",
            priority="urgent",
            click=CANONICAL_THEATRE_URL,
        )
        print(f"HEALTH WARNING: {health_error}")
    elif not health_error and previous_health_error:
        send_text_notification(
            "Odyssey bot healthy again",
            "AMC health checks are passing and the monitor is reading CityWalk ticket links normally again.",
            priority="default",
            click=CANONICAL_THEATRE_URL,
        )
        print("Health recovered.")

    # The parser was materially upgraded. On its first v2 run, establish a fresh
    # baseline so existing showtimes do not look like a giant new ticket drop.
    migration_baseline = previous_version < STATE_VERSION
    if migration_baseline or not initialized:
        save_state({
            "version": STATE_VERSION,
            "initialized": True,
            "active": current if not health_error else previous_active,
            "health_error": health_error,
        })
        print(
            f"V{STATE_VERSION} baseline saved with {len(current)} current clickable showtime link(s); "
            "no ticket alert sent for pre-existing listings."
        )
        return 0 if not health_error else 2

    new_items = [item for key, item in current.items() if key not in previous_active]
    if new_items:
        send_ticket_notification(new_items)
        print(f"Sent phone notification for {len(new_items)} newly clickable showtime(s).")

    # On a health failure, don't erase prior state based on potentially incomplete data.
    if health_error:
        next_active = dict(previous_active)
        next_active.update(current)
    else:
        next_active = current

    save_state({
        "version": STATE_VERSION,
        "initialized": True,
        "active": next_active,
        "health_error": health_error,
    })

    print(
        f"Checked {successful_dates} dates; {len(current)} clickable Odyssey IMAX 70mm link(s); "
        f"{len(new_items)} newly available; health={'OK' if not health_error else 'WARNING'}."
    )
    return 0 if not health_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
