# Odyssey IMAX 70mm — Universal CityWalk date monitor

This GitHub Actions monitor watches **The Odyssey** in **IMAX 70mm** at Universal Cinema, an AMC Theatre at CityWalk Hollywood and sends an ntfy alert only when a **brand-new future calendar date** first appears.

It tracks **dates, not individual showtimes**. If a known date gains another showtime, changes availability, sells out, restocks, disappears, or later reappears, there is no alert.

## Live source

AMC's own website currently challenges/blocks requests from GitHub-hosted runners, including headless-browser requests. The monitor therefore uses the showtime feed behind CityWalk's Fandango ticketing page:

- Fandango theater ID: AAAWX
- Chain: AMC
- Venue: Universal Cinema, an AMC Theatre
- Feed: Fandango theaterMovieShowtimes NAPI

The request uses the same theater-page Referer and AJAX-style headers used by Fandango's web page. Each returned payload is validated against theater ID AAAWX before it is trusted.

## What qualifies

A date qualifies only when a future CityWalk listing matches **The Odyssey** and the individual showtime explicitly carries an **IMAX 70MM** film-format marker.

The parser deliberately rejects:

- plain IMAX / digital IMAX
- standard 70mm without IMAX
- another movie's IMAX 70mm label
- past or expired performances

A sold-out future IMAX 70mm showing still makes the date qualify. Availability is not part of date detection, so a ticket or seat restock cannot create a false alert.

## Date-only state

Version 7 stores:

- initialized — whether a successful v7 baseline exists
- ever_seen_dates — every qualifying date that has already been baselined or successfully alerted
- pending_dates — newly detected dates whose ntfy delivery still needs to succeed
- current_dates — qualifying dates found on the latest complete scan
- health_error — the most recent scan/delivery problem
- last_attempt_at — most recent attempted run
- last_success_at — most recent complete successful scan
- source — current live source

Once a date enters ever_seen_dates, it stays known. That is what makes extra times, restocks, disappearance, and reappearance silent.

## Baseline and failures

The first completely successful run after a rewrite is intentionally silent. It records every currently listed qualifying future date as the baseline so existing listings are not mistaken for newly added dates.

A partial or failed sweep is never treated as an empty result. If any requested day cannot be read reliably, the run records a health warning and leaves the successful comparison baseline intact.

If an ntfy delivery fails, the new date remains in pending_dates and is retried while that date is still listed.

## Schedule

The workflow requests a run about every five minutes and scans the next 21 future calendar dates in Los Angeles time. GitHub Actions schedules can start later than the requested minute.

Code changes also trigger the workflow immediately for validation. State-only commits do not retrigger it.

The workflow runs unit tests before the live scan and writes state changes back to state.json.

## Current verified baseline

The first successful v7 live scan on October 8, 2026 found qualifying Odyssey IMAX 70mm listings on:

- October 9, 2026
- October 10, 2026
- October 11, 2026
- October 12, 2026
- October 13, 2026
- October 14, 2026

Each currently had 2:00 PM, 6:00 PM, and 10:00 PM listings. These dates are now baseline dates and will not alert merely because their times or availability change.

The next alert should happen only when a qualifying future calendar date appears that is not already in ever_seen_dates.

## Phone notifications

The repository uses the existing NTFY_TOPIC GitHub Actions secret.

A valid alert contains the new calendar date, all currently listed qualifying showtimes on that date, and the Fandango ticketing link when available.

This bot does not log in, reserve seats, or purchase tickets.
