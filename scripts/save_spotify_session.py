import base64
from pathlib import Path

from playwright.sync_api import sync_playwright


STATE_FILE = Path("spotify_state.json")
B64_FILE = Path("spotify_state_b64.txt")

CHART_URL = "https://charts.spotify.com/charts/view/regional-global-daily/latest"


def main():
    print("Opening browser. Log into Spotify manually.")
    print("After the chart page fully loads, come back here and press Enter.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(
            viewport={"width": 1365, "height": 900}
        )

        page = context.new_page()
        page.goto(CHART_URL, wait_until="domcontentloaded", timeout=90000)

        input("Press Enter here after you are fully logged in and the chart page loads... ")

        context.storage_state(path=str(STATE_FILE))
        browser.close()

    encoded = base64.b64encode(STATE_FILE.read_bytes()).decode("utf-8")
    B64_FILE.write_text(encoded, encoding="utf-8")

    print("")
    print("Saved:")
    print(f"- {STATE_FILE}")
    print(f"- {B64_FILE}")
    print("")
    print("Copy the entire contents of spotify_state_b64.txt into a GitHub secret named:")
    print("SPOTIFY_STORAGE_STATE_B64")


if __name__ == "__main__":
    main()
