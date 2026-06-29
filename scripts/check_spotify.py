import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


EASTERN = ZoneInfo("America/New_York")

STATE_PATH = "data/state.json"
DEBUG_DIR = "debug"
SESSION_STATE_PATH = "spotify_storage_state.json"

DEFAULT_CHART_SLUG = "regional-global-daily"
DEFAULT_PUBLIC_CHART_URL = f"https://charts.spotify.com/charts/view/{DEFAULT_CHART_SLUG}/latest"

PEAK_START_HOUR = 9
PEAK_START_MINUTE = 45
PEAK_END_HOUR = 10
PEAK_END_MINUTE = 30

CHECK_INTERVAL_SECONDS_DURING_PEAK = 60

REQUEST_TIMEOUT_SECONDS = 25
PAGE_TIMEOUT_MS = 90000
NETWORK_WAIT_MS = 70000


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
    GitHub Actions runs every 5 minutes.

    During the peak window, this script stays alive and checks every 60 seconds.
    It stops before the next scheduled GitHub run should begin.
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


def save_debug(page, name: str) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)

    try:
        page.screenshot(path=f"{DEBUG_DIR}/{name}.png", full_page=True)
    except Exception as exc:
        print(f"Could not save screenshot: {exc}")

    try:
        html = page.content()
        with open(f"{DEBUG_DIR}/{name}.html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print(f"Could not save HTML: {exc}")


def write_storage_state_from_cookies() -> str:
    """
    Creates a Playwright browser session from Spotify cookies copied from your normal browser.

    Required GitHub secret:
    - SPOTIFY_SP_DC

    Optional GitHub secret:
    - SPOTIFY_SP_KEY
    """
    sp_dc = os.getenv("SPOTIFY_SP_DC", "").strip()
    sp_key = os.getenv("SPOTIFY_SP_KEY", "").strip()

    if not sp_dc:
        raise RuntimeError(
            "Missing SPOTIFY_SP_DC. Add your Spotify sp_dc cookie as a GitHub Actions repository secret."
        )

    expires = int(time.time()) + 60 * 60 * 24 * 365

    cookies = [
        {
            "name": "sp_dc",
            "value": sp_dc,
            "domain": ".spotify.com",
            "path": "/",
            "expires": expires,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]

    if sp_key:
        cookies.append(
            {
                "name": "sp_key",
                "value": sp_key,
                "domain": ".spotify.com",
                "path": "/",
                "expires": expires,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        )

    storage_state = {
        "cookies": cookies,
        "origins": [],
    }

    Path(SESSION_STATE_PATH).write_text(
        json.dumps(storage_state, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote storage state from Spotify cookies to {SESSION_STATE_PATH}")
    return SESSION_STATE_PATH


def fetch_chart_json_with_browser() -> dict:
    """
    Opens Spotify Charts using saved Spotify cookies instead of trying to log in every time.
    Then captures the internal Spotify chart API response.
    """
    public_url = get_public_chart_url()
    target_substring = get_target_api_substring()
    storage_state_path = write_storage_state_from_cookies()

    print(f"Public chart URL: {public_url}")
    print(f"Target API substring: {target_substring}")

    service_responses_seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            storage_state=storage_state_path,
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        def log_response(response):
            if "charts-spotify-com-service.spotify.com" in response.url:
                service_responses_seen.append(response.url)
                print(f"Spotify service response: {response.status} {response.url}")

        page.on("response", log_response)

        def is_target_response(response) -> bool:
            return (
                "charts-spotify-com-service.spotify.com" in response.url
                and target_substring in response.url
                and response.request.method == "GET"
            )

        try:
            with page.expect_response(is_target_response, timeout=NETWORK_WAIT_MS) as response_info:
                page.goto(public_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            captured_response = response_info.value

            print(f"Captured response URL: {captured_response.url}")
            print(f"Captured response status: {captured_response.status}")

            if captured_response.status >= 400:
                save_debug(page, "bad_chart_response")
                body_preview = captured_response.text()[:500]
                raise RuntimeError(
                    f"Captured API returned HTTP {captured_response.status}. Preview: {body_preview}"
                )

            data = captured_response.json()
            browser.close()
            return data

        except PlaywrightTimeoutError:
            save_debug(page, "no_chart_response")
            browser.close()
            raise RuntimeError(
                "Could not capture chart API response. "
                "The Spotify cookie may be missing, expired, copied from the wrong browser profile, "
                "or not enough by itself. "
                f"Spotify service responses seen: {service_responses_seen[:10]}"
            )

        except Exception:
            save_debug(page, "failure")
            browser.close()
            raise


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


def send_discord_update(
    old_hash: str,
    new_hash: str,
    summary: dict,
    first_run: bool = False,
) -> None:
    webhook_url = get_discord_webhook_url()

    if not webhook_url:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is not set. "
            "Not saving changed hash because no Discord alert can be sent."
        )

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

    payload = {
        "content": "@everyone" if os.getenv("DISCORD_PING_EVERYONE", "false").lower() == "true" else "",
        "embeds": [
            {
                "title": title,
                "description": description,
                "url": public_chart_url,
                "color": color,
                "fields": fields,
                "footer": {
                    "text": "Spotify Chart Monitor"
                },
            }
        ],
    }

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    print(f"Discord status: {response.status_code}")

    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")


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

    if not state.get("initialized"):
        print("First run. Initializing state.")

        state["last_hash"] = new_hash
        state["last_seen_chart_date"] = summary.get("chart_date", "")
        state["last_seen_title"] = summary.get("title", "")
        state["last_seen_top_entry"] = summary.get("top_entry", "")
        state["last_checked_at"] = iso_utc()
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
        return True

    print("Change detected.")

    # Send Discord before saving the new hash.
    # If Discord fails, the next run retries instead of silently missing the alert.
    send_discord_update(
        old_hash=old_hash,
        new_hash=new_hash,
        summary=summary,
        first_run=False,
    )

    state["last_hash"] = new_hash
    state["last_seen_chart_date"] = summary.get("chart_date", "")
    state["last_seen_title"] = summary.get("title", "")
    state["last_seen_top_entry"] = summary.get("top_entry", "")
    state["last_checked_at"] = iso_utc()
    state["last_changed_at"] = iso_utc()
    state["initialized"] = True

    save_state(state)

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
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
