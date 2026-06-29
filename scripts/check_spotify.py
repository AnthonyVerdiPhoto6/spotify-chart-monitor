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

TARGET_ALIAS = "REGIONAL_GLOBAL_DAILY"

PUBLIC_CHART_URL = "https://charts.spotify.com/charts/view/regional-global-daily/latest"
OVERVIEW_URL = "https://charts.spotify.com/charts/overview/global"
CHARTS_API_URL = "https://charts-spotify-com-service.spotify.com/auth/v1/overview/GLOBAL"

TOKEN_URLS = [
    "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
    "https://open.spotify.com/api/token?reason=transport&productType=web_player",
]

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
    if not is_peak_window():
        return False

    elapsed_seconds = (now_utc() - start_time).total_seconds()
    return elapsed_seconds < 260


def get_spotify_cookie_header() -> str:
    sp_dc = os.getenv("SPOTIFY_SP_DC", "").strip()
    sp_key = os.getenv("SPOTIFY_SP_KEY", "").strip()

    if not sp_dc:
        raise RuntimeError(
            "Missing SPOTIFY_SP_DC. Add your Spotify sp_dc cookie as a GitHub Actions secret."
        )

    parts = [f"sp_dc={sp_dc}"]

    if sp_key:
        parts.append(f"sp_key={sp_key}")

    return "; ".join(parts)


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


def base_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }


def fetch_spotify_access_token() -> str:
    cookie_header = get_spotify_cookie_header()
    headers = base_headers()
    headers["Cookie"] = cookie_header
    headers["Referer"] = "https://open.spotify.com/"

    last_error = None

    for token_url in TOKEN_URLS:
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                print(f"Fetching Spotify access token from: {token_url}")
                print(f"Attempt {attempt}/{REQUEST_RETRIES}")

                response = requests.get(
                    token_url,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )

                print(f"Token HTTP status: {response.status_code}")

                if response.status_code >= 400:
                    raise RuntimeError(f"Token endpoint returned HTTP {response.status_code}: {response.text[:300]}")

                data = response.json()

                token = (
                    data.get("accessToken")
                    or data.get("access_token")
                    or data.get("token")
                )

                if not token:
                    raise RuntimeError(f"No access token found in token response. Keys: {list(data.keys())}")

                print("Successfully got Spotify access token.")
                return token

            except Exception as exc:
                last_error = exc
                print(f"Token request failed: {exc}")

                if attempt < REQUEST_RETRIES:
                    time.sleep(REQUEST_RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Could not get Spotify access token. Last error: {last_error}")


def fetch_overview_json() -> dict:
    token = fetch_spotify_access_token()
    cookie_header = get_spotify_cookie_header()

    headers = base_headers()
    headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Cookie": cookie_header,
            "Origin": "https://charts.spotify.com",
            "Referer": OVERVIEW_URL,
            "App-Platform": "WebPlayer",
        }
    )

    last_error = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            print(f"Fetching Spotify Charts overview API. Attempt {attempt}/{REQUEST_RETRIES}")
            print(f"API URL: {CHARTS_API_URL}")

            response = requests.get(
                CHARTS_API_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            print(f"Charts API HTTP status: {response.status_code}")

            if response.status_code >= 400:
                raise RuntimeError(f"Charts API returned HTTP {response.status_code}: {response.text[:500]}")

            data = response.json()
            print("Successfully fetched overview JSON.")
            return data

        except Exception as exc:
            last_error = exc
            print(f"Overview request failed: {exc}")

            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Could not fetch Spotify overview JSON. Last error: {last_error}")


def extract_target_chart_from_overview(data: dict) -> dict:
    sections = data.get("sections", [])

    if not isinstance(sections, list):
        raise RuntimeError("Overview JSON did not contain a normal sections list.")

    for section in sections:
        charts = section.get("charts", [])
        if not isinstance(charts, list):
            continue

        for chart in charts:
            metadata = chart.get("chartMetadata", {})
            alias = metadata.get("alias", "")

            if alias == TARGET_ALIAS:
                print(f"Found target chart alias: {TARGET_ALIAS}")
                return chart

    raise RuntimeError(f"Could not find target chart alias in overview JSON: {TARGET_ALIAS}")


def extract_chart_summary(chart: dict) -> dict:
    summary = {
        "title": "Daily Top Songs: Global",
        "chart_date": "",
        "top_entry": "",
        "top_artist": "",
        "top_streams": "",
    }

    chart_metadata = chart.get("chartMetadata") or {}

    readable_title = chart_metadata.get("readableTitle")
    if readable_title:
        summary["title"] = readable_title

    dimensions = chart_metadata.get("dimensions") or {}
    latest_date = dimensions.get("latestDate")

    if latest_date:
        summary["chart_date"] = str(latest_date)

    first_entry = chart.get("firstEntry")

    if isinstance(first_entry, dict):
        track_metadata = first_entry.get("trackMetadata") or {}
        chart_entry_data = first_entry.get("chartEntryData") or {}

        track_name = track_metadata.get("trackName")

        artists = track_metadata.get("artists") or []
        artist_names = []

        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, dict) and artist.get("name"):
                    artist_names.append(artist["name"])

        if track_name and artist_names:
            summary["top_entry"] = f"{track_name} by {', '.join(artist_names)}"
            summary["top_artist"] = ", ".join(artist_names)
        elif track_name:
            summary["top_entry"] = track_name

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

    if first_run:
        title = "Spotify monitor initialized"
        description = "The first chart snapshot has been saved. Future changes will trigger alerts."
        color = 0x808080
    else:
        title = "Spotify Charts updated"
        description = "Daily Top Songs: Global changed on Spotify Charts."
        color = 0x1DB954

    fields = [
        {
            "name": "Detected",
            "value": readable_eastern_time(),
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
                "url": PUBLIC_CHART_URL,
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

    overview_data = fetch_overview_json()
    target_chart = extract_target_chart_from_overview(overview_data)

    normalized = normalize_for_hash(target_chart)
    new_hash = sha256_text(normalized)
    old_hash = state.get("last_hash", "")

    summary = extract_chart_summary(target_chart)

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

        if os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true":
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
    print(f"Target alias: {TARGET_ALIAS}")
    print(f"Charts API URL: {CHARTS_API_URL}")
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
