import asyncio

from pyppeteer import launch
from pyppeteer.errors import TimeoutError as PyppeteerTimeoutError


TRACKING_URL = "https://www.cma-cgm.com/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


async def track_container(raw_container_number: str) -> dict:

    browser = await launch(
        headless=False,
        executablePath=CHROME_PATH,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    try:
        page = await browser.newPage()

        await page.setUserAgent(USER_AGENT)

        print("[INFO] Opening CMA-CGM tracking page...")

        await page.goto(
            TRACKING_URL,
            {
                "waitUntil": "domcontentloaded",
                "timeout": 30000,
            },
        )

        print("[INFO] Page loaded.")

        await page.waitForSelector(
            '[data-cy="tracking-search-input"]',
            {
                "timeout": 20000,
            },
        )

        print("[INFO] Tracking input found.")

        await page.type(
            '[data-cy="tracking-search-input"]',
            raw_container_number,
        )

        print(
            f"[INFO] Container number entered: "
            f"{raw_container_number}"
        )

        return {
            "status": "success",
            "container_number": raw_container_number,
        }

    except PyppeteerTimeoutError:
        raise Exception(
            "Timeout while loading CMA-CGM tracking page."
        )

    finally:
        await browser.close()


if __name__ == "__main__":

    container_number = "CMAU1234567"

    try:
        result = asyncio.run(
            track_container(container_number)
        )

        print(result)

    except Exception as e:
        print(f"Error: {e}")