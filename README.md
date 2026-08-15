# Odyssey IMAX 70mm — Universal CityWalk monitor

Cloud monitor for **The Odyssey – IMAX 70mm Event** at **Universal Cinema AMC at CityWalk Hollywood**.

It runs in GitHub Actions on a 5-minute schedule, checks AMC's official showtime pages, remembers showtime IDs it has already seen, and sends an ntfy phone push when a newly purchasable matching showtime appears.

## Phone setup

1. Install the **ntfy** app on your phone.
2. Subscribe to your private topic.
3. In this GitHub repo, open **Settings → Secrets and variables → Actions → New repository secret**.
4. Create a secret named `NTFY_TOPIC` with your ntfy topic as the value.
5. Open **Actions → Odyssey 70mm CityWalk Watch → Run workflow** once.

The first successful run establishes the baseline and intentionally sends no ticket alert. Later newly appearing matching showtimes trigger a push.

The bot only checks availability; it does not log in, reserve seats, or purchase tickets.
