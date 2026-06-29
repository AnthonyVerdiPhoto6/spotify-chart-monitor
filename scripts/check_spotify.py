import csv
import hashlib
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


EASTERN = ZoneInfo("America/New_York")

STATE_PATH = "data/state.json"

DEFAULT_CSV_URL = "https://spotifycharts.com/regional/global/daily/latest/download"
DEFAULT_PUBLIC_CHART_URL = "https://charts.spotify.com/charts/view/regional-global-daily/latest"

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

    During peak window, we keep this one job alive and check every 60 seconds.
    We stop before the next 5-minute job should start.
    """
    if not is_peak_window():
        return False

    elapsed_seconds = (now_utc() - start_time).total_seconds()
    return elapsed_seconds < 260


def get_csv_url() -> str:
    return os.getenv("CSV_URL", DEFAULT_CSV_URL).strip()


def get_public_chart_url() -> str:
    return os.getenv("PUBLIC_CHART_URL", DEFAULT_PUBLIC_CHART_URL).strip()


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


def fetch_csv() -> tuple[str, dict]:
    url = get_csv_url()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }

    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            print(f"Fetching Spotify CSV. Attempt {attempt}/{REQUEST_RETRIES}")
            print(f"CSV URL: {url}")

            response = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )

            print(f"HTTP status: {response.status_code}")
            print(f"Final URL: {response.url}")

            response.raise_for_status()

            text = response.text

            if not text.strip():
                raise RuntimeError("Spotify returned an empty response.")

            lowered = text.lower()
            if "<html" in lowered[:500]:
                raise RuntimeError(
                    "Spotify returned HTML instead of CSV. "
                    "This likely means the CSV endpoint is blocked or redirected."
                )

            metadata = {
                "final_url": response.url,
                "last_modified": response.headers.get("Last-Modified", ""),
                "content_type": response.headers.get("Content-Type", ""),
            }

            return text, metadata

        except Exception as exc:
            last_error = exc
            print(f"Request failed: {exc}")

            if attempt < REQUEST_RETRIES:
                print(f"Sleeping {REQUEST_RETRY_SLEEP_SECONDS} seconds before retry.")
                time.sleep(REQUEST_RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Failed to fetch Spotify CSV after retries: {last_error}")


def stable_hash(text: str) -> str:
    """
    Normalize line endings before hashing so Windows/Linux line endings do not matter.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_csv_summary(csv_text: str, metadata: dict) -> dict:
    summary = {
        "title": "Daily Top Songs: Global",
        "chart_date": "",
        "top_entry": "",
        "top_artist": "",
        "top_streams": "",
        "entry_count": "",
        "final_url": metadata.get("final_url", ""),
        "last_modified": metadata.get("last_modified", ""),
    }

    cleaned = csv_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Spotify CSVs are usually:
    # Position, Track Name, Artist, Streams, URL
    # but sometimes have extra intro/comment lines.
    lines = [line for line in cleaned.split("\n") if line.strip()]

    header_index = None

    for i, line in enumerate(lines):
        lower = line.lower()
        if "position" in lower and ("track name" in lower or "artist" in lower):
            header_index = i
            break

    if header_index is None:
        print("Could not find normal CSV header. Using raw hash only.")
        return summary

    usable_csv = "\n".join(lines[header_index:])
    reader = csv.DictReader(io.StringIO(usable_csv))
    rows = list(reader)

    summary["entry_count"] = str(len(rows))

    if rows:
        first = rows[0]

        track = (
            first.get("Track Name")
            or first.get("track_name")
            or first.get("Track")
            or ""
        )

        artist = (
            first.get("Artist")
            or first.get("Artists")
            or first.get("artist")
            or ""
        )

        streams = (
            first.get("Streams")
            or first.get("streams")
            or ""
        )

        if track and artist:
            summary["top_entry"] = f"{track} by {artist}"
        elif track:
            summary["top_entry"] = track
        elif artist:
            summary["top_entry"] = artist

        summary["top_artist"] = artist
        summary["top_streams"] = streams.replace(",", "")

    # Date may not be in the CSV itself. Sometimes final_url changes from latest to a dated URL.
    final_url = summary["final_url"]
    if "/daily/" in final_url:
        piece = final_url.split("/daily/", 1)[-1].split("/", 1)[0]
        if piece and piece != "latest":
            summary["chart_date"] = piece

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
            "Not saving a changed hash because no Discord alert can be sent."
        )

    detected_time = readable_eastern_time()
    public_chart_url = get_public_chart_url()

    if first_run:
        title = "Spotify monitor initialized"
        description = "The first chart snapshot has been saved. Future changes will trigger alerts."
        color = 0x808080
    else:
        title = "Spotify Charts updated"
        description = "A change was detected in the monitored Spotify chart CSV."
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
    ]

    if summary.get("chart_date"):
        fields.append(
            {
                "name": "Chart date",
                "value": summary["chart_date"],
                "inline": True,
            }
        )

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

    if summary.get("last_modified"):
        fields.append(
            {
                "name": "Last-Modified header",
                "value": summary["last_modified"],
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


def check_once() -> bool:
    state = load_state()

    csv_text, metadata = fetch_csv()
    new_hash = stable_hash(csv_text)
    old_hash = state.get("last_hash", "")

    summary = parse_csv_summary(csv_text, metadata)

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
        print("State file will not be updated, so no unnecessary commit will be created.")
        return True

    print("Change detected.")

    # Important:
    # Send Discord BEFORE saving new hash.
    # If Discord fails, we do not save the new hash, so the next run will retry the alert.
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
    print(f"CSV URL: {get_csv_url()}")
    print(f"Public chart URL: {get_public_chart_url()}")
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
