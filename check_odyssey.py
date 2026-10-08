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
STATE_VERSION = 6
PACIFIC = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
AMC_BROWSER_CHANNEL = os.getenv("AMC_BROWSER_CHANNEL", "chrome").strip() or "chrome"
AMC_NAV_TIMEOUT_MS = int(os.getenv("AMC_NAV_TIMEOUT_MS", "30000"))
AMC_SETTLE_MS = int(os.getenv("AMC_SETTLE_MS", "3000"))

THEATRE_URL = (
    "https://www.amctheatres.com/movie-theatres/los-angeles/"
    "universal-cinema-amc-at-citywalk-hollywood/showtimes"
)
CANONICAL_THEATRE_URL = THEATRE_URL
SHOWTIME_LINK_RE = re.compile(r"/showtimes/(\d+)(?:(?:/(?:seats|tickets))?(?:[?#]|$))", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b", re.I)
ODYSSEY_RE = re.compile(r"\bthe\s+odyssey\b", re.I)
IMAX_70_RE = re.compile(r"\bimax(?:®|\(r\))?\s*70\s*mm\b", re.I)
BLOCK_PAGE_MARKERS = (
    "global safety net",
    "access denied",
    "verify you are human",
    "checking your browser",
    "attention required",
)


def norm(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split()).lower()


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
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_state()


def save_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def legacy_seen_dates(state: dict) -> set[str]:
    dates = set(state.get("ever_seen_dates") or [])
    dates.update(state.get("seen_dates") or [])
    legacy_showings = state.get("showings") or state.get("active") or state.get("seen") or {}
    if isinstance(legacy_showings, dict):
        for item in legacy_showings.values():
            if isinstance(item, dict) and item.get("date"):
                dates.add(str(item["date"]))
    return dates


def page_is_real_amc_theatre_page(text: str) -> bool:
    low = norm(text)
    if any(marker in low for marker in BLOCK_PAGE_MARKERS):
        return False
    return "universal cinema" in low and "citywalk" in low


def local_showtime_text(a) -> str:
    best = " ".join(a.stripped_strings)
    node = a
    for _ in range(5):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(getattr(node, "stripped_strings", []))
        if len(text) > 1800:
            break
        if TIME_RE.search(text):
            best = text
    return best


GENERIC_HEADING_WORDS = (
    "imax", "70mm", "70 mm", "showtime", "reserved seating",
    "closed caption", "audio description", "assistive listening",
)


def context_is_single_odyssey_group(node) -> bool:
    headings = [norm(h.get_text(" ", strip=True)) for h in node.find_all(re.compile(r"^h[1-6]$"))]
    if not headings:
        return True
    has_odyssey_heading = any(ODYSSEY_RE.search(h) for h in headings)
    if not has_odyssey_heading:
        return True
    for heading in headings:
        if ODYSSEY_RE.search(heading):
            continue
        if any(word in heading for word in GENERIC_HEADING_WORDS):
            continue
        # A second non-format heading usually means we climbed into a container
        # holding another movie card. Fail closed instead of mixing evidence.
        return False
    return True


def smallest_matching_context(a) -> str | None:
    """Return the smallest nearby DOM context proving Odyssey + IMAX 70mm."""
    node = a
    for _ in range(12):
        node = getattr(node, "parent", None)
        if node is None or getattr(node, "name", None) in {"body", "html"}:
            break
        text = " ".join(getattr(node, "stripped_strings", []))
        if len(text) > 5000:
            break
        if ODYSSEY_RE.search(text) and IMAX_70_RE.search(text):
            if context_is_single_odyssey_group(node):
                return text
            return None
    return None


def page_has_odyssey_imax70_signal(soup: BeautifulSoup) -> bool:
    for text_node in soup.find_all(string=ODYSSEY_RE):
        node = getattr(text_node, "parent", None)
        for _ in range(10):
            if node is None or getattr(node, "name", None) in {"body", "html"}:
                break
            text = " ".join(getattr(node, "stripped_strings", []))
            if len(text) > 6000:
                break
            if IMAX_70_RE.search(text) and TIME_RE.search(text):
                return context_is_single_odyssey_group(node)
            node = getattr(node, "parent", None)
    return False


def parse_listing_page(html: str, show_date: date) -> tuple[list[dict], int, bool]:
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
        if not smallest_matching_context(a):
            continue

        one_showtime_text = local_showtime_text(a)
        tm = TIME_RE.search(" ".join(a.stripped_strings)) or TIME_RE.search(one_showtime_text)
        display_time = tm.group(1).upper().replace(" ", "") if tm else "time listed on AMC"
        showing_url = urljoin(CANONICAL_THEATRE_URL, href.split("?")[0])
        found[sid] = {
            "date": show_date.isoformat(),
            "time": display_time,
            "showtime_id": sid,
            "url": showing_url,
        }

    return list(found.values()), len(generic_ids), page_has_odyssey_imax70_signal(soup)


class AMCBrowser:
    """Load AMC's official pages with the Chrome browser installed on GitHub runners."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "AMCBrowser":
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if AMC_BROWSER_CHANNEL:
            launch_args["channel"] = AMC_BROWSER_CHANNEL
        self._browser = self._playwright.chromium.launch(**launch_args)
        self._context = self._browser.new_context(
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1365, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _load(self, url: str) -> str:
        last_problem = ""
        for attempt in range(2):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=AMC_NAV_TIMEOUT_MS)
                self._page.wait_for_timeout(AMC_SETTLE_MS + attempt * 2000)
                body = self._page.locator("body").inner_text(timeout=5000)
                html = self._page.content()
                if page_is_real_amc_theatre_page(body):
                    return html
                last_problem = f"AMC returned a challenge/non-theatre page ({len(body)} chars)"
            except Exception as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(last_problem or "AMC page did not load")

    def fetch_date(self, show_date: date) -> tuple[list[dict], int, bool]:
        url = f"{THEATRE_URL}?date={show_date.isoformat()}&premium-offering=imax"
        html = self._load(url)
        return parse_listing_page(html, show_date)


def scan_future_dates(browser: AMCBrowser, today: date) -> dict[str, list[dict]]:
    showings_by_date: dict[str, list[dict]] = {}

    for offset in range(1, DAYS_AHEAD + 1):
        d = today + timedelta(days=offset)
        try:
            items, generic_count, signal = browser.fetch_date(d)
        except Exception as exc:
            # A browser/navigation failure usually means AMC blocked the source,
            # not that one calendar date is special. Abort immediately so a blocked
            # run never spends minutes hammering every date and never changes state.
            raise RuntimeError(f"{d}: {type(exc).__name__}: {exc}") from exc

        if signal and not items:
            raise RuntimeError(
                f"{d}: AMC page contains Odyssey + IMAX 70mm + showtime text, "
                "but no matching AMC showtime link was parsed"
            )

        if items:
            showings_by_date[d.isoformat()] = items
            times = ", ".join(sorted(item["time"] for item in items))
            print(f"{d}: Odyssey IMAX 70mm — {times}")
        else:
            print(f"{d}: no Odyssey IMAX 70mm ({generic_count} AMC showtime links on page)")

    return showings_by_date


def send_new_date_notification(new_dates: list[str], showings_by_date: dict[str, list[dict]]) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("GitHub secret NTFY_TOPIC is not configured")

    lines: list[str] = []
    click = CANONICAL_THEATRE_URL
    for date_string in sorted(new_dates):
        items = sorted(showings_by_date.get(date_string, []), key=lambda item: item["time"])
        times = ", ".join(item["time"] for item in items) or "showtimes listed on AMC"
        lines.append(f"{date_string} — {times}")
        if items and click == CANONICAL_THEATRE_URL:
            click = items[0]["url"]

    response = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=(
            "AMC added The Odyssey IMAX 70mm on a NEW FUTURE DATE at Universal CityWalk:\n"
            + "\n".join(lines)
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
    migration_baseline = state.get("version") != STATE_VERSION or not state.get("initialized", False)
    ever_seen = legacy_seen_dates(state)
    pending = set(state.get("pending_dates") or [])
    pending = {d for d in pending if d > today.isoformat()}

    try:
        with AMCBrowser() as browser:
            showings_by_date = scan_future_dates(browser, today)
    except Exception as exc:
        health_error = f"AMC browser scan failed: {type(exc).__name__}: {exc}"
        print(f"HEALTH WARNING: {health_error}")
        state.update({
            "version": STATE_VERSION,
            "initialized": False if migration_baseline else bool(state.get("initialized", False)),
            "ever_seen_dates": sorted(ever_seen),
            "pending_dates": sorted(pending),
            "health_error": health_error,
            "last_attempt_at": now.isoformat(),
        })
        save_state(state)
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as report:
                report.write("### Odyssey date monitor: WARNING\nAMC could not be scanned reliably; no baseline or alert state was changed.\n")
        return 2

    current_dates = set(showings_by_date)

    if migration_baseline:
        # A rewrite/recovery run is deliberately silent. Establish a clean snapshot so
        # already-existing listings do not produce a false "new date" notification.
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
        }
        save_state(state)
        print(f"V{STATE_VERSION} silent baseline complete: {len(current_dates)} qualifying future date(s).")
        return 0

    new_dates = current_dates - ever_seen
    pending.update(new_dates)
    deliverable = sorted(pending & current_dates)

    # Dates already known on a successful scan stay known forever. This is what makes
    # added times, sold-out changes, disappearance, and later reappearance silent.
    ever_seen.update(current_dates - set(deliverable))

    if deliverable:
        try:
            send_new_date_notification(deliverable, showings_by_date)
        except Exception as exc:
            health_error = f"notification delivery failed ({type(exc).__name__})"
            print(f"HEALTH WARNING: {health_error}; retaining {len(deliverable)} pending date(s).")
            state = {
                "version": STATE_VERSION,
                "initialized": True,
                "ever_seen_dates": sorted(ever_seen),
                "pending_dates": sorted(pending),
                "current_dates": sorted(current_dates),
                "health_error": health_error,
                "last_attempt_at": now.isoformat(),
                "last_success_at": now.isoformat(),
            }
            save_state(state)
            return 2
        else:
            ever_seen.update(deliverable)
            pending.difference_update(deliverable)
            print(f"Sent alert for {len(deliverable)} brand-new future date(s).")

    state = {
        "version": STATE_VERSION,
        "initialized": True,
        "ever_seen_dates": sorted(ever_seen),
        "pending_dates": sorted(pending),
        "current_dates": sorted(current_dates),
        "health_error": "",
        "last_attempt_at": now.isoformat(),
        "last_success_at": now.isoformat(),
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
        f"Successful date-only scan: {len(current_dates)} current qualifying date(s); "
        f"{len(deliverable)} new date(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
