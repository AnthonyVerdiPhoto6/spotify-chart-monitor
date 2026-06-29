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
DEBUG_DIR = "debug"

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

    start = dt.replace(hour=PEAK_START_HOUR, minute=PEAK_START_MINUTE, second=0, microsecond=0)
    end = dt.replace(hour=PEAK_END_HOUR, minute=PEAK_END_MINUTE, second=0, microsecond=0)

    return start <= dt <= end


def should_keep_looping_this_run(start_time: datetime) -> bool:
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


def get_spotify_email() -> str:
    return os.getenv("SPOTIFY_EMAIL", "").strip()


def get_spotify_password() -> str:
    return os.getenv("SPOTIFY_PASSWORD", "").strip()


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
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


def click_if_visible(page, selector: str, timeout: int = 3000) -> bool:
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout)
        locator.click(timeout=timeout)
        return True
    except Exception:
        return False


def fill_first_working_selector(page, selectors: list[str], value: str, label: str) -> None:
    last_error = None

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=10000)
            locator.fill(value)
            print(f"Filled {label} using selector: {selector}")
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Could not fill {label}. Last error: {last_error}")


def click_first_working_selector(page, selectors: list[str], label: str) -> None:
    last_error = None

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=10000)
            locator.click()
            print(f"Clicked {label} using selector: {selector}")
            return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Could not click {label}. Last error: {last_error}")


def login_to_spotify(page) -> None:
    email = get_spotify_email()
    password = get_spotify_password()

    if not email or not password:
        raise RuntimeError(
            "Spotify login required but SPOTIFY_EMAIL or SPOTIFY_PASSWORD is missing. "
            "Add both as GitHub Actions repository secrets."
        )

    print("Opening Spotify login page.")
    page.goto(
        "https://accounts.spotify.com/en/login",
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    # Cookie/consent buttons sometimes appear.
    click_if_visible(page, "button:has-text('Accept Cookies')")
    click_if_visible(page, "button:has-text('Accept')")
    click_if_visible(page, "button:has-text('Continue without Accepting')")

    print("Filling Spotify email.")

    fill_first_working_selector(
        page,
        [
            "input#login-username",
            "input[data-testid='login-username']",
            "input[name='username']",
            "input[type='email']",
            "input[type='text']",
        ],
        email,
        "Spotify email",
    )

    # Spotify sometimes uses a two-step flow:
    # Step 1: enter email
    # Step 2: click Continue
    # Step 3: password field appears
    print("Checking whether Spotify requires Continue after email.")

    clicked_continue = False

    continue_selectors = [
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button[data-testid='login-button']",
        "button[type='submit']",
    ]

    for selector in continue_selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=3000):
                locator.click()
                print(f"Clicked email Continue/Next using selector: {selector}")
                clicked_continue = True
                break
        except Exception:
            pass

    if clicked_continue:
        page.wait_for_timeout(3000)

    print("Filling Spotify password.")

    fill_first_working_selector(
        page,
        [
            "input#login-password",
            "input[data-testid='login-password']",
            "input[name='password']",
            "input[type='password']",
        ],
        password,
        "Spotify password",
    )

    print("Submitting Spotify login.")

    click_first_working_selector(
        page,
        [
            "button#login-button",
            "button[data-testid='login-button']",
            "button[type='submit']",
            "button:has-text('Log In')",
            "button:has-text('Log in')",
            "button:has-text('Continue')",
        ],
        "Spotify login button",
    )

    print("Waiting after login submit.")

    # Give Spotify time to redirect or show a logged-in state.
    page.wait_for_timeout(5000)

    current_url = page.url
    print(f"URL after login submit: {current_url}")

    # If still on accounts.spotify.com, try waiting longer.
    if "accounts.spotify.com" in current_url:
        try:
            page.wait_for_url(
                lambda url: "accounts.spotify.com" not in url,
                timeout=PAGE_TIMEOUT_MS,
            )
            print(f"Login redirected to: {page.url}")
        except PlaywrightTimeoutError:
            save_debug(page, "login_timeout")
            raise RuntimeError(
                "Spotify login did not complete. This may mean wrong credentials, "
                "captcha, email verification, 2FA/security check, or Spotify blocked GitHub's login attempt. "
                "Check debug/login_timeout.png artifact."
            )
    else:
        print(f"Login appears complete. Current URL: {page.url}")


def fetch_chart_json_with_logged_in_browser() -> dict:
    public_url = get_public_chart_url()
    target_substring = get_target_api_substring()

    print(f"Public chart URL: {public_url}")
    print(f"Target API substring: {target_substring}")

    service_responses_seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
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

        def log_response(response):
            if "charts-spotify-com-service.spotify.com" in response.url:
                service_responses_seen.append(response.url)
                print(f"Spotify service response: {response.status} {response.url}")

        page.on("response", log_response)

        try:
            login_to_spotify(page)

            print("Opening chart page after login.")

            def is_target_response(response) -> bool:
                return (
                    "charts-spotify-com-service.spotify.com" in response.url
                    and target_substring in response.url
                    and response.request.method == "GET"
                )

            with page.expect_response(is_target_response, timeout=NETWORK_WAIT_MS) as response_info:
                page.goto(public_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            captured_response = response_info.value

            status = captured_response.status
            response_url = captured_response.url

            print(f"Captured response URL: {response_url}")
            print(f"Captured response status: {status}")

            if status >= 400:
                body_preview = captured_response.text()[:500]
                save_debug(page, "bad_chart_response")
                raise RuntimeError(
                    f"Captured Spotify API response returned HTTP {status}. "
                    f"Preview: {body_preview}"
                )

            try:
                data = captured_response.json()
            except Exception as exc:
                body_preview = captured_response.text()[:500]
                save_debug(page, "invalid_json_response")
                raise RuntimeError(
                    f"Captured response was not valid JSON. Preview: {body_preview}"
                ) from exc

            browser.close()
            return data

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


def send_discord_update(old_hash: str, new_hash: str, summary: dict, first_run: bool = False) -> None:
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
        {"name": "Detected", "value": detected_time, "inline": False},
        {"name": "Chart", "value": summary.get("title") or "Unknown", "inline": True},
        {"name": "Chart date", "value": summary.get("chart_date") or "Unknown", "inline": True},
    ]

    if summary.get("top_entry"):
        fields.append({"name": "#1 entry", "value": summary["top_entry"], "inline": False})

    if summary.get("top_streams"):
        fields.append({"name": "#1 streams", "value": format_streams(summary["top_streams"]), "inline": True})

    if summary.get("entry_count"):
        fields.append({"name": "Entries found", "value": summary["entry_count"], "inline": True})

    fields.append({"name": "Hash", "value": f"`{old_hash[:10] or 'none'}` → `{new_hash[:10]}`", "inline": False})

    payload = {
        "content": "@everyone" if os.getenv("DISCORD_PING_EVERYONE", "false").lower() == "true" else "",
        "embeds": [
            {
                "title": title,
                "description": description,
                "url": public_chart_url,
                "color": color,
                "fields": fields,
                "footer": {"text": "Spotify Chart Monitor"},
            }
        ],
    }

    response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)

    print(f"Discord status: {response.status_code}")

    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")


def check_once() -> bool:
    state = load_state()

    data = fetch_chart_json_with_logged_in_browser()

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
            send_discord_update("", new_hash, summary, first_run=True)

        return True

    if new_hash == old_hash:
        print("No change detected.")
        return True

    print("Change detected.")

    # Send Discord before saving the new hash.
    # If Discord fails, the next run retries instead of silently missing the alert.
    send_discord_update(old_hash, new_hash, summary, first_run=False)

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
