import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


EASTERN = ZoneInfo("America/New_York")

STATE_PATH = "data/state.json"

MONITOR_URL = "https://kworb.net/spotify/country/global_daily.html"
PUBLIC_CHART_URL = "https://charts.spotify.com/charts/view/regional-global-daily/latest"

ACTIVE_START_HOUR = 9
ACTIVE_START_MINUTE = 0
ACTIVE_END_HOUR = 23
ACTIVE_END_MINUTE = 59

PEAK_START_HOUR = 9
PEAK_START_MINUTE = 45
PEAK_END_HOUR = 10
PEAK_END_MINUTE = 30

CHECK_INTERVAL_SECONDS_DURING_PEAK = 60

REQUEST_TIMEOUT_SECONDS = 25
REQUEST_RETRIES = 3
REQUEST_RETRY_SLEEP_SECONDS = 5


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_eastern() -> datetime:
    return now_utc().astimezone(EASTERN)


def iso_utc() -> str:
    return now_utc().isoformat(timespec="seconds")


def readable_eastern_time() -> str:
    return now_eastern().strftime("%Y-%m-%d %I:%M:%S %p %Z")

def is_active_window(dt: datetime | None = None) -> bool:
    """
    Active monitoring window: 9:00 AM to 4:00 PM Eastern.

    Uses < end time, so monitoring stops after the 3:55 PM run.
    """
    if dt is None:
        dt = now_eastern()

    start = dt.replace(
        hour=ACTIVE_START_HOUR,
        minute=ACTIVE_START_MINUTE,
        second=0,
        microsecond=0,
    )

    end = dt.replace(
        hour=ACTIVE_END_HOUR,
        minute=ACTIVE_END_MINUTE,
        second=0,
        microsecond=0,
    )

    return start <= dt < end


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


def get_discord_webhook_url() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {
            "last_hash": "",
            "last_seen_title": "",
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


def fetch_page() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            print(f"Fetching monitor page. Attempt {attempt}/{REQUEST_RETRIES}")
            print(f"URL: {MONITOR_URL}")

            response = requests.get(
                MONITOR_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            print(f"HTTP status: {response.status_code}")

            response.raise_for_status()

            text = response.text

            if not text.strip():
                raise RuntimeError("Monitor page returned empty HTML.")

            return text

        except Exception as exc:
            last_error = exc
            print(f"Request failed: {exc}")

            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Failed to fetch monitor page after retries: {last_error}")


def clean_for_hash(html: str) -> str:
    # Basic normalization so harmless whitespace differences do not matter.
    lines = [line.strip() for line in html.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_basic_summary(html: str) -> dict:
    summary = {
        "title": "Spotify Daily Chart - Global",
        "top_line": "",
    }

    # Light parsing without extra dependencies.
    # Kworb rows contain artist/song text in the HTML, but exact tags can change.
    # This keeps the monitor robust even if parsing is imperfect.
    lowered = html.lower()

    if "spotify daily chart - global" in lowered:
        summary["title"] = "Spotify Daily Chart - Global"

    # Try to grab visible title-ish first meaningful lines.
    cleaned = clean_for_hash(html)
    for line in cleaned.split("\n"):
        if " - " in line and "<" not in line and len(line) < 160:
            summary["top_line"] = line
            break

    return summary


def send_discord_update(old_hash: str, new_hash: str, summary: dict, first_run: bool = False) -> None:
    webhook_url = get_discord_webhook_url()

    if not webhook_url:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is not set. Not saving changed hash because no Discord alert can be sent."
        )

    if first_run:
        title = "Spotify monitor initialized"
        description = "The first chart snapshot has been saved. Future changes will trigger alerts."
        color = 0x808080
    else:
        title = "Spotify chart page updated"
        description = "The monitored Global Spotify Daily chart page changed."
        color = 0x1DB954

    fields = [
        {
            "name": "Detected",
            "value": readable_eastern_time(),
            "inline": False,
        },
        {
            "name": "Monitor source",
            "value": MONITOR_URL,
            "inline": False,
        },
        {
            "name": "Official chart page",
            "value": PUBLIC_CHART_URL,
            "inline": False,
        },
    ]

    if summary.get("top_line"):
        fields.append(
            {
                "name": "Detected text",
                "value": summary["top_line"][:900],
                "inline": False,
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
                "url": MONITOR_URL,
                "color": color,
                "fields": fields,
                "footer": {
                    "text": "GitHub Spotify Monitor"
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

    html = fetch_page()
    cleaned = clean_for_hash(html)
    new_hash = sha256_text(cleaned)
    old_hash = state.get("last_hash", "")
    summary = extract_basic_summary(html)

    print(f"Old hash: {old_hash}")
    print(f"New hash: {new_hash}")
    print(f"Summary: {json.dumps(summary, indent=2)}")

    if not state.get("initialized"):
        print("First run. Initializing state.")

        state["last_hash"] = new_hash
        state["last_seen_title"] = summary.get("title", "")
        state["last_checked_at"] = iso_utc()
        state["last_changed_at"] = iso_utc()
        state["initialized"] = True

        save_state(state)

        if os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true":
            send_discord_update("", new_hash, summary, first_run=True)

        return True

    if new_hash == old_hash:
        print("No change detected.")
        return True

    print("Change detected.")

    send_discord_update(old_hash, new_hash, summary, first_run=False)

    state["last_hash"] = new_hash
    state["last_seen_title"] = summary.get("title", "")
    state["last_checked_at"] = iso_utc()
    state["last_changed_at"] = iso_utc()
    state["initialized"] = True

    save_state(state)

    return True


def main() -> int:
    print("Spotify/Kworb Chart Monitor starting.")
    print(f"Eastern time: {readable_eastern_time()}")
    print(f"Monitor URL: {MONITOR_URL}")
    print(f"Active window: {is_active_window()}")
    print(f"Peak window active: {is_peak_window()}")

    start_time = now_utc()

    try:
        if not is_active_window():
            print("Outside active monitoring window. No check will run.")
            return 0

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
