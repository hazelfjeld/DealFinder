from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from playwright.sync_api import sync_playwright, Page

search = input("what would you like to search for?")

BASE_URL = "https://www.pawnamerica.com"
SEARCH_URL = f"https://www.pawnamerica.com/Shop?query={str(search)}"


@dataclass(frozen=True)
class Product:
    name: str
    price: Optional[float]
    url: str


PRICE_PATTERN = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.(\d{2}))?")


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


def normalize_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"{BASE_URL}{href}"
    return f"{BASE_URL}/{href}"


def extract_products_from_dom(page: Page) -> List[Dict[str, Any]]:
    """
    Extract products by scanning links and pulling name + price from a nearby container.
    This is intentionally "structure-agnostic" to survive CSS/classname changes.
    """
    extraction_script = r"""
    () => {
      const isProbablyProductLink = (href) => {
        if (!href) return false;
        const h = href.toLowerCase();
        // Common patterns on ecommerce sites:
        if (h.includes('/product')) return true;
        if (h.includes('/item')) return true;
        if (h.includes('/p/')) return true;
        // PawnAmerica sometimes uses simple paths; keep it a bit permissive:
        if (h.includes('/shop/') || h.includes('/shop?')) return false;
        return false;
      };

      const priceRegex = /\$\s*\d[\d,]*(?:\.\d{2})?/;

      const links = Array.from(document.querySelectorAll('a[href]'))
        .filter(a => isProbablyProductLink(a.getAttribute('href')));

      const results = [];
      const seen = new Set();

      for (const link of links) {
        const href = link.getAttribute('href');
        if (!href) continue;

        // Build a "card" candidate by walking up a few parents and grabbing text
        let container = link;
        let containerText = '';
        for (let i = 0; i < 7; i++) {
          if (!container || !container.parentElement) break;
          containerText = (container.innerText || '').trim();
          if (priceRegex.test(containerText)) break;
          container = container.parentElement;
        }

        // Name heuristics: link text, otherwise image alt nearby
        let name = (link.innerText || '').trim();
        if (!name) {
          const img = link.querySelector('img[alt]') || (container ? container.querySelector('img[alt]') : null);
          if (img && img.getAttribute('alt')) name = img.getAttribute('alt').trim();
        }

        const priceMatch = containerText.match(priceRegex);
        const priceText = priceMatch ? priceMatch[0] : '';

        // Avoid junk/duplicates
        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({
          href,
          name,
          priceText,
          // for highlighting later:
          domPathHint: null
        });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def highlight_detected_products(page: Page) -> None:
    """
    Adds outlines + small index badges to product links the script considers products.
    """
    highlight_script = r"""
    () => {
      const isProbablyProductLink = (href) => {
        if (!href) return false;
        const h = href.toLowerCase();
        if (h.includes('/product')) return true;
        if (h.includes('/item')) return true;
        if (h.includes('/p/')) return true;
        return false;
      };

      const candidates = Array.from(document.querySelectorAll('a[href]'))
        .filter(a => isProbablyProductLink(a.getAttribute('href')));

      candidates.forEach((a, idx) => {
        a.style.outline = '3px solid #00ff88';
        a.style.outlineOffset = '2px';
        a.style.position = 'relative';

        // Badge
        const badge = document.createElement('span');
        badge.textContent = String(idx + 1);
        badge.style.position = 'absolute';
        badge.style.top = '-10px';
        badge.style.left = '-10px';
        badge.style.background = '#00ff88';
        badge.style.color = '#000';
        badge.style.padding = '2px 6px';
        badge.style.borderRadius = '999px';
        badge.style.fontSize = '12px';
        badge.style.fontWeight = '700';
        badge.style.zIndex = '999999';

        // Only add if not already added
        if (!a.dataset._highlighted) {
          a.dataset._highlighted = '1';
          a.appendChild(badge);
        }
      });

      return candidates.length;
    }
    """
    total_highlighted = page.evaluate(highlight_script)
    print(f"[debug] highlighted {total_highlighted} candidate product links in the browser")


def coerce_products(raw_items: List[Dict[str, Any]]) -> List[Product]:
    products: List[Product] = []
    seen_urls = set()

    for item in raw_items:
        href = (item.get("href") or "").strip()
        if not href:
            continue

        full_url = normalize_url(href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        name = (item.get("name") or "").strip()
        price_text = (item.get("priceText") or "").strip()
        price = parse_price_to_float(price_text) if price_text else None

        # If the name is empty, still keep it (we’ll show it), but try to avoid pure blanks
        if not name:
            name = "(no name found)"

        products.append(Product(name=name, price=price, url=full_url))

    # Sort: cheapest first, unknown prices last
    products.sort(key=lambda p: (p.price is None, p.price if p.price is not None else 10**12))
    return products


def save_to_csv(products: List[Product], csv_path: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["name", "price", "url"])
        for product in products:
            writer.writerow([product.name, "" if product.price is None else f"{product.price:.2f}", product.url])


def main() -> None:
    print("Launching browser…")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, slow_mo=50)
        context = browser.new_context(viewport={"width": 1200, "height": 900})
        page = context.new_page()

        print(f"Opening: {SEARCH_URL}")
        page.goto(SEARCH_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # give JS a moment

        # Try to wait for *something* product-ish to appear
        page.wait_for_timeout(1500)

        # Highlight what we think are products
        highlight_detected_products(page)

        # Extract
        raw_items = extract_products_from_dom(page)
        products = coerce_products(raw_items)

        print("\n=== Parsed Products (name / price / link) ===")
        for index, product in enumerate(products, start=1):
            price_display = "N/A" if product.price is None else f"${product.price:.2f}"
            print(f"{index:>3}. {price_display:<10} {product.name} | {product.url}")

        csv_path = "pawnamerica_pokemon.csv"
        save_to_csv(products, csv_path)
        print(f"\nSaved: {csv_path}")

        input("\nBrowser is open and highlighted. Press ENTER here to close it…")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

