from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

STATE_PATH = Path("state.json")
STATE_VERSION = 7
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.20"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# Universal Cinema AMC at CityWalk Hollywood on Fandango.
# Fandango's theater page is the ticketing fallback because AMC currently
# challenges/blocks GitHub-hosted requests, including headless Chrome.
FANDANGO_THEATER_ID = "AAAWX"
FANDANGO_CHAIN = "AMC"
FANDANGO_SLUG = "universal-cinema-amc-at-citywalk-hollywood-aaawx"
FANDANGO_PAGE = f"https://www.fandango.com/{FANDANGO_SLUG}/theater-page"
FANDANGO_API = (
    "https://www.fandango.com/napi/theaterMovieShowtimes/"
    f"{FANDANGO_THEATER_ID}"
)
AMC_THEATRE_URL = (
    "https://www.amctheatres.com/movie-theatres/los-angeles/"
    "universal-cinema-amc-at-citywalk-hollywood/showtimes"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
ODYSSEY_RE = re.compile(r"\bthe\s+odyssey\b", re.I)
IMAX_70_RE = re.compile(r"\bimax(?:®|\(r\))?\s*70\s*mm\b", re.I)


def default_state() -> dict:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "ever_seen_dates": [],
        "pending_dates": [],
        "current_dates": [],
        "health_error": "",
        "last_attempt_at": None,
        "last_success_at": None,
        "source": "fandango",
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_state()


def save_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def legacy_seen_dates(state: dict) -> set[str]:
    """Carry forward any dates known by older state schemas."""
    dates = set(state.get("ever_seen_dates") or [])
    dates.update(state.get("seen_dates") or [])
    legacy_showings = state.get("showings") or state.get("active") or state.get("seen") or {}
    if isinstance(legacy_showings, dict):
        for item in legacy_showings.values():
            if isinstance(item, dict) and item.get("date"):
                dates.add(str(item["date"]))
    return dates


def fandango_page_url(show_date: date | str) -> str:
    d = show_date.isoformat() if isinstance(show_date, date) else str(show_date)
    return f"{FANDANGO_PAGE}?format=IMAX+70MM&date={d}"


def fetch_day(session: requests.Session, show_date: date) -> dict:
    d = show_date.isoformat()
    response = session.get(
        FANDANGO_API,
        params={
            "chainCode": FANDANGO_CHAIN,
            "startDate": d,
            "isdesktop": "true",
            "partnerRestrictedTicketing": "",
        },
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": fandango_page_url(d),
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()

    view_model = data.get("viewModel")
    if not isinstance(view_model, dict):
        raise RuntimeError("Fandango response did not contain viewModel")

    details = ((view_model.get("theater") or {}).get("details") or {})
    returned_id = str(details.get("id") or "").upper()
    returned_chain = str(details.get("chainCode") or "").upper()
    returned_name = str(details.get("name") or "")

    if returned_id != FANDANGO_THEATER_ID:
        raise RuntimeError(
            f"Fandango returned unexpected theater id {returned_id!r}"
        )
    if returned_chain and returned_chain != FANDANGO_CHAIN:
        raise RuntimeError(
            f"Fandango returned unexpected theater chain {returned_chain!r}"
        )
    if "universal cinema" not in returned_name.lower():
        raise RuntimeError(
            f"Fandango returned unexpected theater name {returned_name!r}"
        )

    movies = view_model.get("movies")
    if movies is None:
        raise RuntimeError("Fandango response did not contain movies")
    if not isinstance(movies, list):
        raise RuntimeError("Fandango movies field was not a list")

    return view_model


def _show_is_imax_70mm(show: dict, group: dict) -> bool:
    """Require an explicit IMAX-70mm marker, never plain IMAX or plain 70mm."""
    formats = [
        str(item.get("filterName") or "")
        for item in (show.get("filmFormat") or [])
        if isinstance(item, dict)
    ]
    if formats:
        return any(IMAX_70_RE.search(value) for value in formats)

    # Older/alternate Fandango payloads may express the same format as an
    # amenity instead of per-showtime filmFormat. Only use this fallback when
    # filmFormat is absent, and require IMAX + 70mm in the same amenity group.
    amenity_names = [
        str(item.get("name") or "")
        for item in (group.get("amenities") or [])
        if isinstance(item, dict)
    ]
    joined = " ".join(amenity_names)
    return bool(IMAX_70_RE.search(joined))


def extract_matching_showtimes(view_model: dict, show_date: date) -> list[dict]:
    """Return Odyssey IMAX 70mm showtimes for exactly one requested date.

    Sold-out listings still count. Availability is deliberately not part of
    date detection, so restocks/status changes cannot create an alert.
    """
    wanted_date = show_date.isoformat()
    found: dict[str, dict] = {}

    for movie in view_model.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        if not ODYSSEY_RE.search(str(movie.get("title") or "")):
            continue

        for variant in movie.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            for group in variant.get("amenityGroups") or []:
                if not isinstance(group, dict):
                    continue
                for show in group.get("showtimes") or []:
                    if not isinstance(show, dict):
                        continue
                    if not _show_is_imax_70mm(show, group):
                        continue

                    # The API is queried one date at a time, but filter by
                    # ticketingDate too if Fandango happens to return spillover.
                    ticketing_date = str(show.get("ticketingDate") or "")
                    if re.match(r"^\d{4}-\d{2}-\d{2}", ticketing_date):
                        if ticketing_date[:10] != wanted_date:
                            continue

                    # Future date listings count even when sold out. The only
                    # things excluded are explicitly past/expired performances.
                    status = str(show.get("type") or "").lower()
                    if status == "pastshowtime" or bool(show.get("expired")):
                        continue

                    time_label = str(show.get("date") or "").strip()
                    if not time_label and "+" in ticketing_date:
                        time_label = ticketing_date.split("+", 1)[1]

                    unique = (
                        ticketing_date
                        or str(show.get("showtimeHashCode") or "")
                        or str(show.get("id") or "")
                        or time_label
                    )
                    if not unique:
                        continue

                    found[unique] = {
                        "date": wanted_date,
                        "time": time_label or "time listed",
                        "status": status or "unknown",
                        "url": str(show.get("ticketingJumpPageURL") or fandango_page_url(wanted_date)),
                        "source": "Fandango",
                    }

    return list(found.values())


def scan_future_dates(session: requests.Session, today: date) -> dict[str, list[dict]]:
    showings_by_date: dict[str, list[dict]] = {}

    for offset in range(1, DAYS_AHEAD + 1):
        d = today + timedelta(days=offset)
        try:
            view_model = fetch_day(session, d)
        except Exception as exc:
            # A partial sweep is not a trustworthy date snapshot. Abort without
            # modifying the successful baseline.
            raise RuntimeError(
                f"{d}: {type(exc).__name__}: {exc}"
            ) from exc

        items = extract_matching_showtimes(view_model, d)
        if items:
            showings_by_date[d.isoformat()] = items
            times = ", ".join(sorted(item["time"] for item in items))
            print(f"{d}: Odyssey IMAX 70mm — {times}")
        else:
            print(f"{d}: no Odyssey IMAX 70mm")

        if REQUEST_DELAY > 0 and offset != DAYS_AHEAD:
            time.sleep(REQUEST_DELAY)

    return showings_by_date


def send_new_date_notification(
    new_dates: list[str],
    showings_by_date: dict[str, list[dict]],
) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    lines: list[str] = []
    click = AMC_THEATRE_URL

    for date_string in sorted(new_dates):
        items = sorted(
            showings_by_date.get(date_string, []),
            key=lambda item: item["time"],
        )
        times = ", ".join(item["time"] for item in items) or "showtimes listed"
        lines.append(f"{date_string} — {times}")
        if items and click == AMC_THEATRE_URL:
            click = items[0]["url"]

    response = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=(
            "NEW The Odyssey IMAX 70mm DATE at Universal CityWalk:\n"
            + "\n".join(lines)
            + "\n\nDate-only alert: added times/restocks on known dates are ignored."
        ).encode("utf-8"),
        headers={
            "Title": "NEW Odyssey IMAX 70mm date",
            "Priority": "urgent",
            "Tags": "ticket",
            "Click": click,
        },
        timeout=20,
    )
    response.raise_for_status()


def main() -> int:
    now = datetime.now(PACIFIC)
    today = now.date()
    state = load_state()

    migration_baseline = (
        state.get("version") != STATE_VERSION
        or not state.get("initialized", False)
    )
    ever_seen = legacy_seen_dates(state)
    pending = {
        d
        for d in set(state.get("pending_dates") or [])
        if d > today.isoformat()
    }

    session = requests.Session()
    try:
        showings_by_date = scan_future_dates(session, today)
    except Exception as exc:
        health_error = f"Fandango scan failed: {type(exc).__name__}: {exc}"
        print(f"HEALTH WARNING: {health_error}")
        state.update({
            "version": STATE_VERSION,
            "initialized": False if migration_baseline else bool(state.get("initialized", False)),
            "ever_seen_dates": sorted(ever_seen),
            "pending_dates": sorted(pending),
            "health_error": health_error,
            "last_attempt_at": now.isoformat(),
            "source": "fandango",
        })
        save_state(state)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as report:
                report.write(
                    "### Odyssey date monitor: WARNING\n"
                    "Fandango could not be scanned reliably; "
                    "no baseline or alert state was changed.\n"
                )
        return 2

    current_dates = set(showings_by_date)

    if migration_baseline:
        ever_seen.update(current_dates)
        pending.clear()
        state = {
            "version": STATE_VERSION,
            "initialized": True,
            "ever_seen_dates": sorted(ever_seen),
            "pending_dates": [],
            "current_dates": sorted(current_dates),
            "health_error": "",
            "last_attempt_at": now.isoformat(),
            "last_success_at": now.isoformat(),
            "source": "fandango",
        }
        save_state(state)
        print(
            f"V{STATE_VERSION} silent baseline complete: "
            f"{len(current_dates)} qualifying future date(s)."
        )
        return 0

    new_dates = current_dates - ever_seen
    pending.update(new_dates)
    deliverable = sorted(pending & current_dates)

    # Every successfully observed/alerted date remains known forever. That makes
    # extra times, availability changes, disappearance, and reappearance silent.
    ever_seen.update(current_dates - set(deliverable))

    if deliverable:
        try:
            send_new_date_notification(deliverable, showings_by_date)
        except Exception as exc:
            health_error = f"notification delivery failed ({type(exc).__name__})"
            print(
                f"HEALTH WARNING: {health_error}; "
                f"retaining {len(deliverable)} pending date(s)."
            )
            state = {
                "version": STATE_VERSION,
                "initialized": True,
                "ever_seen_dates": sorted(ever_seen),
                "pending_dates": sorted(pending),
                "current_dates": sorted(current_dates),
                "health_error": health_error,
                "last_attempt_at": now.isoformat(),
                "last_success_at": now.isoformat(),
                "source": "fandango",
            }
            save_state(state)
            return 2
        else:
            ever_seen.update(deliverable)
            pending.difference_update(deliverable)
            print(
                f"Sent alert for {len(deliverable)} "
                "brand-new future date(s)."
            )

    state = {
        "version": STATE_VERSION,
        "initialized": True,
        "ever_seen_dates": sorted(ever_seen),
        "pending_dates": sorted(pending),
        "current_dates": sorted(current_dates),
        "health_error": "",
        "last_attempt_at": now.isoformat(),
        "last_success_at": now.isoformat(),
        "source": "fandango",
    }
    save_state(state)

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as report:
            report.write(
                "### Odyssey date monitor: OK\n"
                f"Current qualifying future dates: {len(current_dates)}. "
                f"New dates this run: {len(deliverable)}.\n"
            )

    print(
        f"Successful date-only scan: {len(current_dates)} "
        f"current qualifying date(s); {len(deliverable)} new date(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
