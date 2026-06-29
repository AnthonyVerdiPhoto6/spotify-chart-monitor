import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


EASTERN = ZoneInfo("America/New_York")

STATE_PATH = "data/state.json"

DEFAULT_CHART_SLUG = "regional-global-daily"

DEFAULT_PUBLIC_CHART_URL = f"https://charts.spotify.com/charts/view/{DEFAULT_CHART_SLUG}/latest"

PEAK_START_HOUR = 9
PEAK_START_MINUTE = 45
PEAK_END_HOUR = 10
PEAK_END_MINUTE = 30

CHECK_INTERVAL_SECONDS_DURING_PEAK = 60

REQUEST_TIMEOUT_SECONDS = 20

PAGE_TIMEOUT_MS = 60000
NETWORK_WAIT_MS = 45000


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_eastern() -> datetime:
    return now_utc().astimezone(EASTERN)


def iso_utc() -> str:
    return now_utc().isoformat(timespec="seconds")


def readable_eastern_time() -> str:
    return now_eastern().strftime("%Y-%m-%d %I:%M:%S %p %Z")


def is_peak_window(dt: datetime | None = None) -> bool:
    if dt is None:
        dt = now_eastern()

    start = dt.replace(
        hour=PEAK_START_HOUR,
        minute=PEAK_START_MINUTE,
        second=0,
        microsecond=0,
    )

    end = dt.replace(
        hour=PEAK_END_HOUR,
        minute=PEAK_END_MINUTE,
        second=0,
        microsecond=0,
    )

    return start <= dt <= end


def should_keep_looping_this_run(start_time: datetime) -> bool:
    """
    GitHub Actions scheduled runs are every 5 minutes.

    During the peak window, this script stays alive and checks every 60 seconds.
    We stop before the next scheduled GitHub run so jobs do not overlap.
    """
    if not is_peak_window():
        return False

    elapsed_seconds = (now_utc() - start_time).total_seconds()
    return elapsed_seconds < 260


def get_chart_slug() -> str:
    return os.getenv("CHART_SLUG", DEFAULT_CHART_SLUG).strip()


def get_public_chart_url() -> str:
    return os.getenv(
        "PUBLIC_CHART_URL",
        f"https://charts.spotify.com/charts/view/{get_chart_slug()}/latest",
    ).strip()


def get_target_api_substring() -> str:
    """
    The browser page calls a URL like:

    https://charts-spotify-com-service.spotify.com/auth/v0/charts/regional-global-daily/latest

    We do not call that URL directly because it returns 401 outside the browser.
    Instead, Playwright opens the real page and captures that response.
    """
    return f"/auth/v0/charts/{get_chart_slug()}/latest"


def get_discord_webhook_url() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {
            "last_hash": "",
            "last_seen_chart_date": "",
            "last_seen_title": "",
            "last_seen_top_entry": "",
            "last_checked_at": "",
            "last_changed_at": "",
            "initialized": False,
        }

    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def normalize_for_hash(data: dict) -> str:
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_chart_json_with_browser() -> dict:
    """
    Opens the actual Spotify Charts page in a headless browser and captures the
    chart API response that the page itself requests.

    This avoids the 401 issue from direct requests.
    """
    public_url = get_public_chart_url()
    target_substring = get_target_api_substring()

    print(f"Opening page: {public_url}")
    print(f"Waiting for API response containing: {target_substring}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        captured_response = None

        def is_target_response(response) -> bool:
            url = response.url
            return (
                "charts-spotify-com-service.spotify.com" in url
                and target_substring in url
                and response.request.method == "GET"
            )

        try:
            with page.expect_response(is_target_response, timeout=NETWORK_WAIT_MS) as response_info:
                page.goto(public_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            captured_response = response_info.value

        except PlaywrightTimeoutError:
            print("Did not catch the target API response during initial page load.")
            print("Trying to wait briefly after page load...")

            page.wait_for_timeout(8000)

            responses_seen = []

            def log_response(response):
                if "charts-spotify-com-service.spotify.com" in response.url:
                    responses_seen.append(response.url)

            page.on("response", log_response)

            page.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(10000)

            browser.close()

            raise RuntimeError(
                "Could not find the Spotify chart API response. "
                f"Expected URL to contain: {target_substring}. "
                f"Spotify service responses seen: {responses_seen[:10]}"
            )

        status = captured_response.status
        response_url = captured_response.url

        print(f"Captured response URL: {response_url}")
        print(f"Captured response status: {status}")

        if status >= 400:
            body_preview = captured_response.text()[:500]
            browser.close()
            raise RuntimeError(
                f"Captured Spotify API response returned HTTP {status}. "
                f"Preview: {body_preview}"
            )

        try:
            data = captured_response.json()
        except Exception as exc:
            body_preview = captured_response.text()[:500]
            browser.close()
            raise RuntimeError(
                f"Captured response was not valid JSON. Preview: {body_preview}"
            ) from exc

        browser.close()
        return data


def extract_chart_summary(data: dict) -> dict:
    summary = {
        "title": "Spotify chart",
        "chart_date": "",
        "top_entry": "",
        "top_artist": "",
        "top_streams": "",
        "entry_count": "",
    }

    chart = data.get("chart", {})
    chart_metadata = data.get("chartMetadata") or chart.get("chartMetadata") or {}

    readable_title = (
        data.get("readableTitle")
        or chart_metadata.get("readableTitle")
        or chart.get("readableTitle")
    )

    if readable_title:
        summary["title"] = readable_title

    dimensions = (
        data.get("dimensions")
        or chart_metadata.get("dimensions")
        or chart.get("dimensions")
        or {}
    )

    latest_date = (
        data.get("latestDate")
        or dimensions.get("latestDate")
        or data.get("date")
        or chart.get("date")
    )

    if latest_date:
        summary["chart_date"] = str(latest_date)

    entries = (
        data.get("entries")
        or data.get("chartEntries")
        or data.get("items")
        or chart.get("entries")
        or chart.get("chartEntries")
        or []
    )

    if isinstance(entries, list):
        summary["entry_count"] = str(len(entries))

    first_entry = None

    if isinstance(entries, list) and entries:
        first_entry = entries[0]
    elif data.get("firstEntry"):
        first_entry = data.get("firstEntry")

    if isinstance(first_entry, dict):
        track_metadata = first_entry.get("trackMetadata") or {}
        artist_metadata = first_entry.get("artistMetadata") or {}
        chart_entry_data = first_entry.get("chartEntryData") or {}

        track_name = track_metadata.get("trackName")
        artist_name = artist_metadata.get("artistName")

        artists = track_metadata.get("artists") or []
        artist_names = []

        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, dict) and artist.get("name"):
                    artist_names.append(artist["name"])

        if track_name and artist_names:
            summary["top_entry"] = f"{track_name} by {', '.join(artist_names)}"
        elif track_name:
            summary["top_entry"] = track_name
        elif artist_name:
            summary["top_entry"] = artist_name
            summary["top_artist"] = artist_name

        ranking_metric = chart_entry_data.get("rankingMetric") or {}
        if ranking_metric.get("value"):
            summary["top_streams"] = str(ranking_metric["value"])

    return summary


def format_streams(value: str) -> str:
    if not value:
        return ""

    try:
        return f"{int(value):,}"
    except ValueError:
        return value


def send_discord_update(old_hash: str, new_hash: str, summary: dict, first_run: bool = False) -> None:
    webhook_url = get_discord_webhook_url()

    if not webhook_url:
        print("No DISCORD_WEBHOOK_URL set. Skipping Discord notification.")
        return

    detected_time = readable_eastern_time()
    public_chart_url = get_public_chart_url()

    if first_run:
        title = "Spotify monitor initialized"
        description = "The first chart snapshot has been saved. Future changes will trigger alerts."
        color = 0x808080
    else:
        title = "Spotify Charts updated"
        description = "A change was detected in the monitored Spotify chart data."
        color = 0x1DB954

    fields = [
        {
            "name": "Detected",
            "value": detected_time,
            "inline": False,
        },
        {
            "name": "Chart",
            "value": summary.get("title") or "Unknown",
            "inline": True,
        },
        {
            "name": "Chart date",
            "value": summary.get("chart_date") or "Unknown",
            "inline": True,
        },
    ]

    if summary.get("top_entry"):
        fields.append(
            {
                "name": "#1 entry",
                "value": summary["top_entry"],
                "inline": False,
            }
        )

    if summary.get("top_streams"):
        fields.append(
            {
                "name": "#1 streams",
                "value": format_streams(summary["top_streams"]),
                "inline": True,
            }
        )

    if summary.get("entry_count"):
        fields.append(
            {
                "name": "Entries found",
                "value": summary["entry_count"],
                "inline": True,
            }
        )

    fields.append(
        {
            "name": "Hash",
            "value": f"`{old_hash[:10] or 'none'}` → `{new_hash[:10]}`",
            "inline": False,
        }
    )

    embed = {
        "title": title,
        "description": description,
        "url": public_chart_url,
        "color": color,
        "fields": fields,
        "footer": {
            "text": "Spotify Chart Monitor"
        },
    }

    payload = {
        "content": "@everyone" if os.getenv("DISCORD_PING_EVERYONE", "false").lower() == "true" else "",
        "embeds": [embed],
    }

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    print(f"Discord status: {response.status_code}")

    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")


def send_discord_error(error_message: str) -> None:
    if os.getenv("ERROR_NOTIFICATIONS", "false").lower() != "true":
        return

    webhook_url = get_discord_webhook_url()

    if not webhook_url:
        return

    payload = {
        "embeds": [
            {
                "title": "Spotify monitor error",
                "description": error_message[:3500],
                "color": 0xFF0000,
                "fields": [
                    {
                        "name": "Time",
                        "value": readable_eastern_time(),
                        "inline": False,
                    }
                ],
            }
        ]
    }

    requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)


def check_once() -> bool:
    state = load_state()

    data = fetch_chart_json_with_browser()

    normalized = normalize_for_hash(data)
    new_hash = sha256_text(normalized)
    old_hash = state.get("last_hash", "")

    summary = extract_chart_summary(data)

    print(f"Old hash: {old_hash}")
    print(f"New hash: {new_hash}")
    print(f"Summary: {json.dumps(summary, indent=2)}")

    state["last_checked_at"] = iso_utc()

    if not state.get("initialized"):
        print("First run. Initializing state.")

        state["last_hash"] = new_hash
        state["last_seen_chart_date"] = summary.get("chart_date", "")
        state["last_seen_title"] = summary.get("title", "")
        state["last_seen_top_entry"] = summary.get("top_entry", "")
        state["last_changed_at"] = iso_utc()
        state["initialized"] = True

        save_state(state)

        notify_first_run = os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"

        if notify_first_run:
            send_discord_update(
                old_hash="",
                new_hash=new_hash,
                summary=summary,
                first_run=True,
            )

        return True

    if new_hash == old_hash:
        print("No change detected.")
        save_state(state)
        return True

    print("Change detected.")

    state["last_hash"] = new_hash
    state["last_seen_chart_date"] = summary.get("chart_date", "")
    state["last_seen_title"] = summary.get("title", "")
    state["last_seen_top_entry"] = summary.get("top_entry", "")
    state["last_changed_at"] = iso_utc()
    state["initialized"] = True

    save_state(state)

    send_discord_update(
        old_hash=old_hash,
        new_hash=new_hash,
        summary=summary,
        first_run=False,
    )

    return True


def main() -> int:
    print("Spotify Chart Monitor starting.")
    print(f"Eastern time: {readable_eastern_time()}")
    print(f"Chart slug: {get_chart_slug()}")
    print(f"Public chart URL: {get_public_chart_url()}")
    print(f"Target API substring: {get_target_api_substring()}")
    print(f"Peak window active: {is_peak_window()}")

    start_time = now_utc()

    try:
        if not is_peak_window():
            check_once()
            return 0

        print("Peak window is active. Checking every 60 seconds during this workflow run.")

        while True:
            check_once()

            if not should_keep_looping_this_run(start_time):
                print("Peak loop finished for this workflow run.")
                break

            print(f"Sleeping {CHECK_INTERVAL_SECONDS_DURING_PEAK} seconds.")
            time.sleep(CHECK_INTERVAL_SECONDS_DURING_PEAK)

        return 0

    except Exception as exc:
        error_message = str(exc)
        print(f"ERROR: {error_message}", file=sys.stderr)

        try:
            send_discord_error(error_message)
        except Exception as discord_exc:
            print(f"Failed to send Discord error notification: {discord_exc}", file=sys.stderr)

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
