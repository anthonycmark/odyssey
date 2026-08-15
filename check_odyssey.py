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
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

THEATRE_URLS = [
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-amc-at-citywalk-hollywood/showtimes",
    "https://www.amctheatres.com/movie-theatres/los-angeles/universal-cinema-an-amc-theatre/showtimes",
]
CANONICAL_THEATRE_URL = THEATRE_URLS[0]

# AMC uses all of these forms at different points in the purchase flow:
# /showtimes/<id>, /showtimes/<id>/seats, and /showtimes/<id>/tickets.
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
        return {"initialized": False, "active": {}, "health_error": ""}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_text_notification(title: str, message: str, priority: str = "high", click: str | None = None) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": "ticket",
    }
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
    lines = [f"{x['date']} — {x['time']}" for x in items[:10]]
    if len(items) > 10:
        lines.append(f"+ {len(items) - 10} more")

    message = (
        "New purchasable Odyssey IMAX 70mm availability at Universal CityWalk:\n"
        + "\n".join(lines)
        + "\n\nTap to open AMC."
    )
    send_text_notification(
        "NEW Odyssey 70mm tickets",
        message,
        priority="urgent",
        click=items[0]["url"],
    )


def nearby_odyssey_70mm_text(a) -> str | None:
    """Find a compact local container tying this link to Odyssey + IMAX + 70mm."""
    node = a
    for _ in range(14):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(getattr(node, "stripped_strings", []))
        if len(text) > 12000:
            break
        low = norm(text)
        if "the odyssey" in low and "imax" in low and ("70mm" in low or "70 mm" in low):
            return text
    return None


def page_has_odyssey_70mm_with_time(soup: BeautifulSoup) -> bool:
    """Independent signal used to catch parser breakage before it becomes a false negative."""
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

        local_text = nearby_odyssey_70mm_text(a)
        if not local_text:
            continue

        tm = TIME_RE.search(" ".join(a.stripped_strings)) or TIME_RE.search(local_text)
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


def verify_candidate(session: requests.Session, item: dict) -> tuple[bool, str]:
    """Confirm each alert candidate on AMC's own showtime-detail page."""
    urls = [item["url"]]
    base_url = f"https://www.amctheatres.com/showtimes/{item['showtime_id']}"
    if base_url not in urls:
        urls.append(base_url)

    last_error = ""
    for url in urls:
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            text = " ".join(soup.stripped_strings)
            low = norm(text)

            format_ok = "imax" in low and ("70mm" in low or "70 mm" in low)
            movie_ok = "the odyssey" in low
            theatre_ok = "universal cinema" in low and "universal" in low
            if not (movie_ok and theatre_ok and format_ok):
                last_error = f"detail page did not verify movie/theatre/format: {url}"
                continue

            if "sold out" in low:
                return False, "sold out"

            # A current listing link plus a verified AMC detail page is enough to treat
            # the showtime as on sale. These markers strengthen that conclusion.
            purchase_markers = (
                "select seats",
                "select tickets",
                "seat map",
                "ticket type",
                "showtime information",
            )
            if any(marker in low for marker in purchase_markers):
                return True, "verified"

            return True, "verified format/detail"
        except Exception as exc:
            last_error = f"detail request failed for {url}: {exc}"

    return False, last_error or "could not verify candidate"


def main() -> int:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })

    state = load_state()
    initialized = bool(state.get("initialized", False))
    previous_active: dict[str, dict] = state.get("active") or state.get("seen") or {}
    previous_health_error = state.get("health_error", "")

    local_today = datetime.now(PACIFIC).date()
    health_errors: list[str] = []

    # CONTROL CHECK: Tomorrow's unfiltered CityWalk page should contain ordinary
    # showtime links. If it doesn't, don't silently interpret that as "no Odyssey".
    control_date = local_today + timedelta(days=1)
    control_url = f"{CANONICAL_THEATRE_URL}?date={control_date.isoformat()}"
    try:
        r = session.get(control_url, timeout=25)
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
        if len(control_ids) == 0:
            health_errors.append(
                "AMC control page returned zero generic showtime links; page parsing may be broken"
            )
    except Exception as exc:
        health_errors.append(f"AMC control page request failed: {exc}")

    raw_candidates: dict[str, dict] = {}
    successful_dates = 0
    total_source_pages = 0

    for offset in range(DAYS_AHEAD + 1):
        d = local_today + timedelta(days=offset)
        date_succeeded = False
        date_candidates: dict[str, dict] = {}
        date_signal = False
        source_generic_counts: list[int] = []

        for base_url in THEATRE_URLS:
            url = f"{base_url}?date={d.isoformat()}&premium-offering=imax"
            try:
                r = session.get(url, timeout=25)
                r.raise_for_status()
                total_source_pages += 1
                date_succeeded = True
                parsed, generic_count, signal = parse_listing_page(r.text, d)
                date_candidates.update(parsed)
                source_generic_counts.append(generic_count)
                date_signal = date_signal or signal
            except Exception as exc:
                print(f"{d} source failed: {url}: {exc}")

        if date_succeeded:
            successful_dates += 1
        else:
            health_errors.append(f"Both AMC listing sources failed for {d}")
            continue

        if date_signal and not date_candidates:
            health_errors.append(
                f"AMC page shows Odyssey + IMAX 70mm + a time on {d}, but parser found no showtime link"
            )

        if date_candidates or date_signal:
            print(
                f"{d}: candidates={len(date_candidates)}, "
                f"generic-links-per-source={source_generic_counts}, signal={date_signal}"
            )

        raw_candidates.update(date_candidates)

    if successful_dates == 0:
        health_errors.append("No AMC dates could be checked")

    verified_current: dict[str, dict] = {}
    for key, item in raw_candidates.items():
        ok, reason = verify_candidate(session, item)
        if ok:
            verified_current[key] = item
            print(f"Verified {key}: {item['time']} ({reason})")
        elif reason == "sold out":
            print(f"Skipped sold-out {key}: {item['time']}")
        else:
            health_errors.append(f"Could not verify AMC candidate {key}: {reason}")

    health_error = " | ".join(sorted(set(health_errors)))

    # Alert immediately if the bot itself is unhealthy, but only once per distinct error.
    if health_error and health_error != previous_health_error:
        send_text_notification(
            "Odyssey bot health warning",
            "The ticket monitor detected a possible AMC parsing/fetch problem:\n\n"
            + health_error
            + "\n\nDo not rely on silent checks until this is fixed.",
            priority="urgent",
            click=CANONICAL_THEATRE_URL,
        )
        print(f"HEALTH WARNING: {health_error}")
    elif not health_error and previous_health_error:
        send_text_notification(
            "Odyssey bot healthy again",
            "AMC health checks are passing again and the monitor is reading CityWalk showtime links normally.",
            priority="default",
            click=CANONICAL_THEATRE_URL,
        )
        print("Health recovered.")

    if not initialized:
        save_state({
            "initialized": True,
            "active": verified_current,
            "health_error": health_error,
        })
        print(f"Baseline saved with {len(verified_current)} verified purchasable showtime(s).")
        return 0 if not health_error else 2

    new_items = [item for key, item in verified_current.items() if key not in previous_active]
    if new_items:
        send_ticket_notification(new_items)
        print(f"Sent phone notification for {len(new_items)} newly available showtime(s).")

    # If health is bad, never erase prior active state based on incomplete data.
    if health_error:
        next_active = dict(previous_active)
        next_active.update(verified_current)
    else:
        next_active = verified_current

    save_state({
        "initialized": True,
        "active": next_active,
        "health_error": health_error,
    })

    print(
        f"Checked {successful_dates} dates via {total_source_pages} AMC source page(s); "
        f"{len(raw_candidates)} raw candidate(s); {len(verified_current)} verified purchasable; "
        f"{len(new_items)} new; health={'OK' if not health_error else 'WARNING'}."
    )
    return 0 if not health_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
