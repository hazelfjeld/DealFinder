from __future__ import annotations

import csv
import re
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template, request
from playwright.sync_api import TimeoutError, sync_playwright

app = Flask(__name__, template_folder="templates")


@dataclass(frozen=True)
class Product:
    name: str
    price: Optional[float]
    url: str
    source: str


PRICE_PATTERN = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.(\d{2}))?")
DEFAULT_SETTLE_MS = 1600

SEARCH_PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "pawnamerica",
        "name": "Pawn America",
        "base_url": "https://www.pawnamerica.com",
        "search_url": "https://www.pawnamerica.com/Shop?query={query}",
    },
    {
        "id": "ebay",
        "name": "eBay",
        "base_url": "https://www.ebay.com",
        "search_url": "https://www.ebay.com/sch/i.html?_nkw={query}",
    },
    {
        "id": "newegg",
        "name": "Newegg",
        "base_url": "https://www.newegg.com",
        "search_url": "https://www.newegg.com/p/pl?d={query}",
    },
    {
        "id": "slickdeals",
        "name": "Slickdeals",
        "base_url": "https://slickdeals.net",
        "search_url": "https://slickdeals.net/newsearch.php?src=SearchBarV2&q={query}&pp=25",
    },
    {
        "id": "walmart",
        "name": "Walmart",
        "base_url": "https://www.walmart.com",
        "search_url": "https://www.walmart.com/search?q={query}",
    },
    {
        "id": "bestbuy",
        "name": "Best Buy",
        "base_url": "https://www.bestbuy.com",
        "search_url": "https://www.bestbuy.com/site/searchpage.jsp?st={query}",
    },
]


def parse_price_to_float(price_text: str) -> Optional[float]:
    """
    Extracts the first $price-looking thing from text and converts it to float.
    Examples:
      "$50" -> 50.0
      "$1,249.99" -> 1249.99
    """
    match = PRICE_PATTERN.search(price_text)
    if not match:
        return None

    dollars_part = match.group(1).replace(",", "")
    cents_part = match.group(2)

    if cents_part is None:
        cents_part = "00"

    return float(f"{dollars_part}.{cents_part}")


def normalize_url(href: str, base_url: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"{base_url}{href}"
    return f"{base_url}/{href}"


def extract_products_from_dom(page) -> List[Dict[str, Any]]:
    """
    Extract products by scanning links and pulling name + price from a nearby container.
    The selector intentionally avoids brittle class names to survive UI changes.
    """
    extraction_script = r"""
    () => {
      const priceRegex = /\$\s*\d[\d,]*(?:\.\d{2})?/;
      const links = Array.from(document.querySelectorAll('a[href]'));

      const results = [];
      const seen = new Set();

      for (const link of links) {
        const href = link.getAttribute('href');
        if (!href) continue;

        let container = link;
        let containerText = '';
        for (let i = 0; i < 7; i++) {
          if (!container || !container.parentElement) break;
          containerText = (container.innerText || '').trim();
          if (priceRegex.test(containerText)) break;
          container = container.parentElement;
        }

        if (!priceRegex.test(containerText)) continue;

        let name = (link.innerText || '').trim();
        if (!name) {
          const img = link.querySelector('img[alt]') || (container ? container.querySelector('img[alt]') : null);
          if (img && img.getAttribute('alt')) name = img.getAttribute('alt').trim();
        }

        const priceMatch = containerText.match(priceRegex);
        const priceText = priceMatch ? priceMatch[0] : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({
          href,
          name,
          priceText,
        });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def coerce_products(raw_items: Iterable[Dict[str, Any]], *, base_url: str, source: str, max_items: int) -> List[Product]:
    products: List[Product] = []
    seen_urls = set()

    for item in raw_items:
        href = (item.get("href") or "").strip()
        if not href:
            continue

        full_url = normalize_url(href, base_url)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        name = (item.get("name") or "").strip()
        price_text = (item.get("priceText") or "").strip()
        price = parse_price_to_float(price_text) if price_text else None

        if not name:
            name = "(no name found)"

        products.append(Product(name=name, price=price, url=full_url, source=source))
        if len(products) >= max_items:
            break

    products.sort(key=lambda p: (p.price is None, p.price if p.price is not None else 10**12))
    return products


def scrape_provider_page(page, provider: Dict[str, Any], query: str, *, max_items: int) -> List[Product]:
    base_url = provider["base_url"]
    search_url = provider["search_url"].format(query=quote_plus(query))

    page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
    page.wait_for_timeout(provider.get("settle_ms", DEFAULT_SETTLE_MS))

    raw_items = extract_products_from_dom(page)
    return coerce_products(
        raw_items,
        base_url=base_url,
        source=provider["name"],
        max_items=max_items,
    )


def scrape_all_providers(query: str, *, max_items_per_site: int = 35) -> List[Product]:
    all_products: List[Product] = []
    started = time.perf_counter()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})

        for provider in SEARCH_PROVIDERS:
            page = context.new_page()
            try:
                products = scrape_provider_page(
                    page,
                    provider,
                    query,
                    max_items=max_items_per_site,
                )
            except TimeoutError:
                products = []
            finally:
                page.close()
            all_products.extend(products)

        context.close()
        browser.close()

    elapsed = time.perf_counter() - started
    print(f"[debug] scraped {len(all_products)} products in {elapsed:.2f}s across {len(SEARCH_PROVIDERS)} sites")
    all_products.sort(key=lambda p: (p.price is None, p.price if p.price is not None else 10**12))
    return all_products


def save_to_csv(products: List[Product], csv_path: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["name", "price", "url", "source"])
        for product in products:
            writer.writerow([
                product.name,
                "" if product.price is None else f"{product.price:.2f}",
                product.url,
                product.source,
            ])


def group_by_source(products: Iterable[Product]) -> Dict[str, List[Product]]:
    grouped: Dict[str, List[Product]] = defaultdict(list)
    for product in products:
        grouped[product.source].append(product)
    return grouped


@app.route("/")
def index():
    query = (request.args.get("q") or "").strip()
    max_items = int(request.args.get("limit") or 35)

    products: List[Product] = []
    grouped: Dict[str, List[Product]] = {}
    if query:
        products = scrape_all_providers(query, max_items_per_site=max_items)
        grouped = group_by_source(products)

    return render_template(
        "index.html",
        query=query,
        products=products,
        grouped=grouped,
        providers=SEARCH_PROVIDERS,
    )


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    max_items = int(request.args.get("limit") or 35)

    if not query:
        return jsonify({"error": "Query is required"}), 400

    products = scrape_all_providers(query, max_items_per_site=max_items)
    return jsonify({"results": [asdict(p) for p in products]})


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
