# Odyssey IMAX 70mm — Universal CityWalk date monitor

Alerts through the existing `NTFY_TOPIC` GitHub secret when a new future calendar date first has a purchasable **The Odyssey — IMAX 70mm** showing at Universal CityWalk. Additional times, seat restocks, and sold-out changes on known dates do not alert. Today's dates are excluded using Los Angeles time.

## Current operational limitation

On September 24, 2026, AMC returned HTTP 403 to both the scheduled monitor and a direct live request. **The monitor cannot detect new dates while AMC blocks its requests.** The code fixes do not remove that restriction. Live parsing and delivery have not been verified end to end.

GitHub's schedule requests a check every five minutes; actual runs can be delayed or skipped. A green workflow alone is not proof that live monitoring works. Check the run summary and `state.json` fields `health_error`, `last_attempt_at`, and `last_success_at`. Health failures remain warnings to avoid repeated failure emails; phone pushes are reserved for new dates.

## Detection and retry behavior

- Preserve existing date history across code updates.
- A first installation establishes a silent baseline only after a complete successful scan.
- Check at least 21 future days and 14 days beyond the latest known future date.
- A newly seen date can alert on its first observation; no prior empty observation is required.
- Keep pending notifications when another page check or delivery fails, and revalidate availability before retrying.
- Stop the scan if the control page fails. Never interpret a failed scan as “no new dates.”
- Match the IMAX 70mm label within the movie container; avoid combining labels from neighbouring movies. This conservative HTML parser still needs validation against live AMC HTML once access is restored.

`python -m unittest -v` runs regression tests with mocked listings and delivery. It does not send a phone notification or prove live access.

## Phone setup

Subscribe to your existing private topic in the ntfy app. The repository's `NTFY_TOPIC` secret must contain that same topic. Use **Actions → Odyssey 70mm CityWalk Watch → Run workflow** to request a check.

This bot does not log in, reserve seats, or purchase tickets.
