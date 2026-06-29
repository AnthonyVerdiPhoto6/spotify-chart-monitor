# Spotify Chart Monitor

This repository monitors a Spotify Charts API endpoint and sends a Discord notification when the chart data changes.

## What it checks

Default chart:

https://charts.spotify.com/charts/view/regional-global-daily/latest

Default API endpoint:

https://charts-spotify-com-service.spotify.com/auth/v0/charts/regional-global-daily/latest

## Schedule

GitHub Actions runs every 5 minutes.

During the peak window, 9:45 AM to 10:30 AM Eastern, the Python script stays alive and checks every 60 seconds during each scheduled run.

## Required GitHub Secret

Create this repository secret:

DISCORD_WEBHOOK_URL

Value:

Your Discord webhook URL.

Do not paste the webhook into the code.

## Optional settings

Inside `.github/workflows/spotify-monitor.yml`, you can change:

- MONITOR_URL
- PUBLIC_CHART_URL
- NOTIFY_ON_FIRST_RUN
- ERROR_NOTIFICATIONS
- DISCORD_PING_EVERYONE

## Manual test

Go to:

Actions → Spotify Chart Monitor → Run workflow

The first run initializes `data/state.json`.

By default, the first run does not send a Discord notification.

## Important note

If Spotify blocks the API endpoint with 401 or 403, this direct API version will not work without additional authentication. In that case, use a browser-based Playwright version that opens the actual Spotify Charts webpage and watches the network response.
