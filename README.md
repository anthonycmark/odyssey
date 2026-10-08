# Odyssey IMAX 70mm — Universal CityWalk date monitor

This GitHub Actions monitor checks AMC's official Universal Cinema AMC at CityWalk Hollywood showtime pages and sends an ntfy alert only when **The Odyssey** appears in **IMAX 70mm** on a **future calendar date that has never previously qualified**.

It tracks **dates, not individual showtimes**. If AMC adds another time on a date that was already known, there is no alert.

## What counts as an alert

A future date qualifies only when the AMC page contains a matching Odyssey showtime in an explicitly IMAX 70mm context. The parser looks for AMC showtime links next to both:

- The Odyssey
- an explicit IMAX 70MM label

Regular IMAX, IMAX with Laser, and standard 70mm do not qualify.

The monitor alerts only for a calendar date that is not already in ever_seen_dates.

It deliberately ignores:

- additional showtimes on an already-known date
- sold-out/full changes
- seat or ticket restocks
- a known date disappearing and later reappearing
- today's date; only future dates are scanned

## AMC access

AMC currently blocks plain server-side HTTP requests with Cloudflare/403 responses. The monitor therefore uses Playwright with the Google Chrome browser already installed on GitHub's Ubuntu runners and loads AMC's official theatre pages as a real browser session.

If AMC serves a challenge page or a date cannot be checked reliably, that run is treated as unhealthy. It does **not** update the successful baseline and it does **not** send a "no new dates" conclusion.

## Baseline behavior

Version 6 uses a fresh date-only state schema.

The first completely successful run after this rewrite is intentionally silent. It records all currently listed qualifying future dates as the baseline so existing listings do not generate false "new date" alerts.

After that:

1. Each successful run scans the next 21 calendar days.
2. It builds the set of qualifying Odyssey IMAX 70mm dates.
3. It compares that set with ever_seen_dates.
4. Only dates never seen before are sent to ntfy.
5. Once successfully notified, a date remains permanently known, so changes on that date never alert again.

Notification failures are retained in pending_dates and retried while that date is still listed.

## Schedule

.github/workflows/watch.yml requests a run about every five minutes. GitHub Actions schedules are not guaranteed to start at the exact requested minute.

The workflow runs the unit tests before the live AMC check and writes state changes back to state.json.

## State fields

- initialized — whether a successful v6 baseline exists
- ever_seen_dates — every qualifying date already baselined or successfully alerted
- pending_dates — newly detected dates whose ntfy delivery has not yet succeeded
- current_dates — qualifying dates found on the latest successful scan
- health_error — most recent live-check or notification problem
- last_attempt_at — most recent attempted run
- last_success_at — most recent complete AMC scan

## Phone notifications

The repository uses the existing NTFY_TOPIC GitHub Actions secret. Subscribe to that private topic in the ntfy app.

A valid notification contains the new calendar date, the currently listed qualifying showtime(s), and an AMC showtime link when one is available.

This bot does not log in, reserve seats, or purchase tickets.
