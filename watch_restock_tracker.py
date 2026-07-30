"""
Watch Restock & Price Tracker
------------------------------
Checks a list of product pages and sends a push notification to your
phone (via ntfy.sh):
  - the moment a product flips from out-of-stock to in-stock
  - the moment a product's price drops to (or below) a target you set

SETUP:
1. pip install requests beautifulsoup4
2. Fill in NTFY_TOPIC and PRODUCTS below
3. Test with: python watch_restock_tracker.py
4. Leave it running, or set it up on GitHub Actions to run in the cloud
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import re
import time
from datetime import datetime

# ══════════════════════════════════════════════
# CONFIG — edit this section only
# ══════════════════════════════════════════════

# The private ntfy topic your phone is subscribed to. Keep this secret --
# anyone who knows it can read/send on it. Already generated for you --
# just subscribe to this EXACT name in the ntfy app on your phone.
# (When run on GitHub Actions, this is overridden by a repository secret
# instead, so the topic name never has to appear in the uploaded code.)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "watch-restock-8623b90e")

# Currency symbol used when reading/printing prices
CURRENCY_SYMBOL = "₹"

# Add one entry per product you want tracked.
# target_price is optional -- set it to a number to also get a "price
# dropped" alert, or leave it as None to only track restocks.
PRODUCTS = [
    {
        "name": "Casio F-91WM-7ADF Youth Digital Watch (Men)",
        "url": "https://dl.flipkart.com/dl/casio-f-91wm-7adf-youth-digital-watch-men/p/itmf3zgfyveympqy?pid=WATESBVU2J8NS6CS&lid=LSTWATESBVU2J8NS6CSOGIBD4&_refId=&_appId=MR",
        "target_price": None,  # e.g. 500 -> alert when price drops to ₹500 or below
    },
    {
        "name": "Casio AE-1200WHD-1AVDF Youth Digital Watch (Men)",
        "url": "https://dl.flipkart.com/s/o991B7uuuN",
        "target_price": 2000,  # alert when price drops to ₹2000 or below
    },
    # Want to track more watches? Copy a block above, paste it below,
    # and change the name/url/target_price. Example:
    # {
    #     "name": "Another Watch",
    #     "url": "https://another-site.com/product-page-2",
    #     "target_price": 1200,
    # },
]

# How often to re-check the page, in minutes
CHECK_INTERVAL_MINUTES = 15

# ══════════════════════════════════════════════
# Logic below -- no need to edit
# ══════════════════════════════════════════════

STATE_FILE = "stock_state.json"

OUT_OF_STOCK_PHRASES = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "notify me when available",
    "notify me",  # Flipkart shows this as the button label when sold out
    "unavailable",
    "out-of-stock",
]

PRICE_REGEX = re.compile(re.escape(CURRENCY_SYMBOL) + r"\s?([\d,]+(?:\.\d+)?)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_stock(url):
    """Returns True if the page looks IN STOCK, False if OUT OF STOCK."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    visible_text = soup.get_text(separator=" ").lower()

    for phrase in OUT_OF_STOCK_PHRASES:
        if phrase in visible_text:
            return False
    return True


def _price_from_jsonld(data):
    """Look for a price inside schema.org-style structured product data --
    most product pages embed this for Google, and it's far more reliable
    than guessing from visible text."""
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        offers = item.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers]
        for offer in offer_list:
            if isinstance(offer, dict) and offer.get("price"):
                try:
                    return float(str(offer["price"]).replace(",", ""))
                except ValueError:
                    continue
    return None


def extract_price_from_scripts(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        price = _price_from_jsonld(data)
        if price is not None:
            return price
    return None


def extract_price_from_text(visible_text):
    """Fallback if no structured data is found: scan the visible page
    text for the first currency amount."""
    match = PRICE_REGEX.search(visible_text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def check_product(url, retries=1):
    """Returns (in_stock: bool, price: float or None).
    Retries once automatically if the site is slow to respond."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Check structured data BEFORE stripping <script> tags away
            price = extract_price_from_scripts(soup)

            for tag in soup(["script", "style"]):
                tag.decompose()
            visible_text = soup.get_text(separator=" ")

            if price is None:
                price = extract_price_from_text(visible_text)

            in_stock = not any(
                phrase in visible_text.lower() for phrase in OUT_OF_STOCK_PHRASES
            )

            return in_stock, price
        except Exception as e:
            last_error = e
    raise last_error


def send_notification(title, message, url, tag="bell"):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Click": url,
            "Priority": "high",
            "Tags": tag,
        },
        timeout=15,
    )


def format_price(price):
    if price is None:
        return "price not found"
    return f"{CURRENCY_SYMBOL}{price:,.0f}"


def run_one_check():
    state = load_state()

    for product in PRODUCTS:
        name = product["name"]
        url = product["url"]
        target_price = product.get("target_price")

        try:
            in_stock, price = check_product(url)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Couldn't check {name}: {e}")
            continue

        prev = state.get(url, {})
        was_in_stock = prev.get("in_stock", False)
        was_below_target = prev.get("was_below_target", False)

        print(f"[{datetime.now():%H:%M:%S}] {name}: "
              f"{'IN STOCK' if in_stock else 'out of stock'}, "
              f"{format_price(price)}")

        if in_stock and not was_in_stock:
            send_notification(
                "Restock alert!",
                f"{name} is back in stock!",
                url,
                tag="tada",
            )
            print("  -> Restock notification sent!")

        is_below_target = (
            target_price is not None
            and price is not None
            and price <= target_price
        )
        if is_below_target and not was_below_target:
            send_notification(
                "Price drop alert!",
                f"{name} dropped to {format_price(price)} "
                f"(your target: {format_price(target_price)})",
                url,
                tag="moneybag",
            )
            print(f"  -> Price drop notification sent! "
                  f"({format_price(price)} <= target {format_price(target_price)})")

        state[url] = {
            "name": name,
            "in_stock": in_stock,
            "was_below_target": is_below_target,
        }

    save_state(state)


def main():
    print(f"Watch tracker started! Checking every "
          f"{CHECK_INTERVAL_MINUTES} minutes.")
    print("Leave this window open. Press Ctrl+C to stop.\n")

    while True:
        run_one_check()
        print(f"Next check in {CHECK_INTERVAL_MINUTES} minutes...\n")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run_one_check()
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nStopped.")
