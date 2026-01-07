from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote_plus

from flask import Flask, Response, jsonify, render_template, request
from playwright.sync_api import TimeoutError, sync_playwright

app = Flask(__name__, template_folder="templates")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dealfinder")

START_TIME = time.time()


@dataclass(frozen=True)
class Product:
    name: str
    price: Optional[float]
    url: str
    source: str
    image_url: Optional[str] = None
    auction_end: Optional[float] = None


PRICE_PATTERN = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.(\d{2}))?")


def env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


DEFAULT_SETTLE_MS = env_int("DEFAULT_SETTLE_MS", 1600, min_value=500, max_value=10000)
NAV_TIMEOUT_MS = env_int("NAV_TIMEOUT_MS", 35000, min_value=10000, max_value=60000)
WAIT_FOR_SELECTOR_TIMEOUT_MS = env_int("WAIT_FOR_SELECTOR_TIMEOUT_MS", 12000, min_value=2000, max_value=30000)
MAX_CONCURRENT_PROVIDERS = env_int("MAX_CONCURRENT_PROVIDERS", 6, min_value=1, max_value=16)
MAX_ITEMS_PER_SITE_DEFAULT = env_int("MAX_ITEMS_PER_SITE", 35, min_value=5, max_value=120)
MAX_QUERY_LENGTH = env_int("MAX_QUERY_LENGTH", 120, min_value=10, max_value=300)
RATE_LIMIT_PER_MINUTE = env_int("RATE_LIMIT_PER_MINUTE", 30, min_value=1, max_value=120)
RATE_LIMIT_WINDOW_SEC = 60
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") != "0"
ALLOWED_SORTS = {"relevance", "price_low", "price_high", "ending_soon"}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "by",
    "deal",
    "deals",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "sale",
    "the",
    "to",
    "with",
}
ACCESSORY_KEYWORDS = {
    "adapter",
    "bag",
    "battery",
    "bundle",
    "cable",
    "case",
    "charger",
    "charging",
    "controller",
    "cord",
    "cover",
    "dock",
    "earbuds",
    "earphones",
    "grip",
    "headset",
    "holder",
    "joystick",
    "keyboard",
    "kit",
    "mouse",
    "mount",
    "pouch",
    "power",
    "protector",
    "protective",
    "screen",
    "shell",
    "skin",
    "stand",
    "strap",
    "stylus",
    "travel",
}

SEARCH_PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "pawnamerica",
        "name": "Pawn America",
        "base_url": "https://www.pawnamerica.com",
        "search_url": "https://www.pawnamerica.com/Shop?query={query}",
        "settle_ms": 2200,
        "wait_for_selector": ".ps-product__title",
        "product_path_patterns": [r"/Product/"],
    },
    {
        "id": "ebay",
        "name": "eBay",
        "base_url": "https://www.ebay.com",
        "search_url": "https://www.ebay.com/sch/i.html?_nkw={query}",
        "product_path_patterns": [r"/itm/"],
    },
    {
        "id": "newegg",
        "name": "Newegg",
        "base_url": "https://www.newegg.com",
        "search_url": "https://www.newegg.com/p/pl?d={query}",
        "product_path_patterns": [r"/p/(?!pl)", r"/Product/"],
    },
    {
        "id": "slickdeals",
        "name": "Slickdeals",
        "base_url": "https://slickdeals.net",
        "search_url": "https://slickdeals.net/newsearch.php?src=SearchBarV2&q={query}&pp=25",
        "wait_for_selector": '.dealCard, .searchResult, a[href*="/f/"], a[href*="/deal/"]',
        "settle_ms": 2400,
        "product_path_patterns": [r"/f/", r"/deal/"],
    },
    {
        "id": "walmart",
        "name": "Walmart",
        "base_url": "https://www.walmart.com",
        "search_url": "https://www.walmart.com/search?q={query}",
        "wait_for_selector": '[data-automation-id="product-tile"], [data-item-id]',
        "settle_ms": 2600,
        "product_path_patterns": [r"/ip/"],
    },
    {
        "id": "bestbuy",
        "name": "Best Buy",
        "base_url": "https://www.bestbuy.com",
        "search_url": "https://www.bestbuy.com/site/searchpage.jsp?st={query}",
        "wait_for_selector": ".sku-item",
        "settle_ms": 2600,
        "product_path_patterns": [r"/site/.+?/\d+\.p"],
    },
    {
        "id": "amazon",
        "name": "Amazon",
        "base_url": "https://www.amazon.com",
        "search_url": "https://www.amazon.com/s?k={query}",
        "product_path_patterns": [r"/dp/", r"/gp/product/"],
    },
    {
        "id": "target",
        "name": "Target",
        "base_url": "https://www.target.com",
        "search_url": "https://www.target.com/s?searchTerm={query}",
        "wait_for_selector": 'a[href*="/p/"]',
        "settle_ms": 2600,
        "product_path_patterns": [r"/p/"],
    },
    {
        "id": "costco",
        "name": "Costco",
        "base_url": "https://www.costco.com",
        "search_url": "https://www.costco.com/CatalogSearch?keyword={query}",
        "product_path_patterns": [r"/product/"],
    },
    {
        "id": "samsclub",
        "name": "Sam's Club",
        "base_url": "https://www.samsclub.com",
        "search_url": "https://www.samsclub.com/s/{query}",
        "product_path_patterns": [r"/p/"],
    },
    {
        "id": "aliexpress",
        "name": "AliExpress",
        "base_url": "https://www.aliexpress.us",
        "search_url": "https://www.aliexpress.us/w/wholesale-{query}.html",
        "wait_for_selector": 'a[href*="/item/"]',
        "settle_ms": 3000,
        "product_path_patterns": [r"/item/"],
    },
    {
        "id": "temu",
        "name": "Temu",
        "base_url": "https://www.temu.com",
        "search_url": "https://www.temu.com/search_result.html?search_key={query}",
        "wait_for_selector": 'a[href*="goods.html"]',
        "settle_ms": 3000,
        "product_path_patterns": [r"/goods.html"],
    },
    {
        "id": "bhphoto",
        "name": "B&H Photo",
        "base_url": "https://www.bhphotovideo.com",
        "search_url": "https://www.bhphotovideo.com/c/search?Ntt={query}",
        "product_path_patterns": [r"/c/product/"],
    },
    {
        "id": "microcenter",
        "name": "Micro Center",
        "base_url": "https://www.microcenter.com",
        "search_url": "https://www.microcenter.com/search/search_results.aspx?Ntt={query}",
        "product_path_patterns": [r"/product/"],
    },
    {
        "id": "gamestop",
        "name": "GameStop",
        "base_url": "https://www.gamestop.com",
        "search_url": "https://www.gamestop.com/search/?q={query}",
        "wait_for_selector": 'a[href*="/products/"]',
        "settle_ms": 2600,
        "product_path_patterns": [r"/products/"],
    },
    {
        "id": "staples",
        "name": "Staples",
        "base_url": "https://www.staples.com",
        "search_url": "https://www.staples.com/search?query={query}",
        "product_path_patterns": [r"/products/"],
    },
    {
        "id": "officedepot",
        "name": "Office Depot",
        "base_url": "https://www.officedepot.com",
        "search_url": "https://www.officedepot.com/catalog/search.do?searchTerm={query}",
        "product_path_patterns": [r"/a/products/"],
    },
    {
        "id": "dell",
        "name": "Dell",
        "base_url": "https://www.dell.com",
        "search_url": "https://www.dell.com/en-us/search/{query}",
        "product_path_patterns": [r"/en-us/shop/"],
    },
    {
        "id": "lenovo",
        "name": "Lenovo",
        "base_url": "https://www.lenovo.com",
        "search_url": "https://www.lenovo.com/us/en/search?query={query}",
        "product_path_patterns": [r"/p/"],
    },
]

_rate_limit_lock = threading.Lock()
_rate_limit_hits: Dict[str, deque[float]] = defaultdict(deque)


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def is_rate_limited(ip: str) -> bool:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return False
    now = time.time()
    with _rate_limit_lock:
        window = _rate_limit_hits[ip]
        while window and (now - window[0]) > RATE_LIMIT_WINDOW_SEC:
            window.popleft()
        if len(window) >= RATE_LIMIT_PER_MINUTE:
            return True
        window.append(now)
    return False


def normalize_query(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw).strip()
    return normalized[:MAX_QUERY_LENGTH]


def parse_int_param(raw_value: str | None, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw_value or default)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def parse_bool_flag(raw_value: str | None, default: bool) -> bool:
    if raw_value is None:
        return default
    return raw_value not in {"0", "false", "False", "no", "off"}


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    return response


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


def tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def query_tokens(query: str) -> List[str]:
    return [token for token in tokenize(query) if len(token) > 1 and token not in STOPWORDS]


def is_relevant_name(name: str, tokens: List[str]) -> bool:
    if not tokens:
        return True
    name_tokens = set(tokenize(name))
    if not name_tokens:
        return False
    return bool(name_tokens.intersection(tokens))


def accessory_penalty(name_tokens: set[str], tokens: List[str]) -> int:
    if not tokens:
        return 0
    accessory_hits = name_tokens.intersection(ACCESSORY_KEYWORDS)
    if not accessory_hits:
        return 0
    if accessory_hits.intersection(tokens):
        return 0
    return 1


def console_boost(name_tokens: set[str], tokens: List[str]) -> int:
    if not {"switch", "lite"}.issubset(tokens):
        return 0
    if {"console", "system", "handheld"}.intersection(name_tokens):
        return 1
    if {"nintendo", "switch", "lite"}.issubset(name_tokens):
        return 1
    return 0


def relevance_sort_key(name: str, tokens: List[str], query: str) -> tuple[int, int, int, int, int]:
    if not tokens:
        return (0, 0, 0, 0, 0)
    name_lower = name.lower()
    name_tokens = set(tokenize(name))
    match_count = sum(1 for token in tokens if token in name_tokens)
    exact_phrase = 1 if query.lower() in name_lower else 0
    missing = len(tokens) - match_count
    boost = console_boost(name_tokens, tokens)
    penalty = accessory_penalty(name_tokens, tokens)
    return (exact_phrase, match_count, missing, boost, penalty)


def is_product_url(url: str, provider: Dict[str, Any]) -> bool:
    patterns = provider.get("product_path_patterns") or []
    if not patterns:
        return True
    return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)


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
        let imageUrl = '';
        const imgTag = link.querySelector('img') || (container ? container.querySelector('img') : null);
        if (imgTag && imgTag.getAttribute('src')) imageUrl = imgTag.getAttribute('src');

        const priceMatch = containerText.match(priceRegex);
        const priceText = priceMatch ? priceMatch[0] : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({
          href,
          name,
          priceText,
          imageUrl,
        });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def extract_newegg_products(page) -> List[Dict[str, Any]]:
    extraction_script = r"""
    () => {
      const items = Array.from(document.querySelectorAll('.item-cell'));
      const results = [];
      const seen = new Set();

      for (const item of items) {
        const title = item.querySelector('a.item-title');
        const price = item.querySelector('.price-current');
        if (!title) continue;

        const href = title.getAttribute('href') || '';
        const name = (title.innerText || '').trim();
        const priceText = (price ? price.innerText : '').trim();
        const image = item.querySelector('img');
        const imageUrl = image ? (image.getAttribute('src') || '') : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def extract_walmart_products(page) -> List[Dict[str, Any]]:
    extraction_script = r"""
    () => {
      const items = Array.from(
        document.querySelectorAll(
          '[data-automation-id="product-tile"], [data-item-id], [data-testid="item-stack"]'
        )
      );
      const results = [];
      const seen = new Set();

      for (const item of items) {
        const link = item.querySelector('a[href*="/ip/"]');
        const title = item.querySelector('[data-automation-id="product-title"], [data-testid="product-title"]') || link;
        const price = item.querySelector(
          '[data-automation-id="product-price"], [data-testid="product-price"], span[itemprop="price"]'
        );

        if (!link) continue;
        const href = link.getAttribute('href') || '';
        const name = (title ? title.innerText : link.innerText || '').trim();
        const priceText = (price ? price.innerText : '').trim();
        const image = item.querySelector('img');
        const imageUrl = image ? (image.getAttribute('src') || '') : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      if (results.length) {
        return results;
      }

      const priceRegex = /\$\s*\d[\d,]*(?:\.\d{2})?/;
      const links = Array.from(document.querySelectorAll('a[href*="/ip/"]'));
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
        let imageUrl = '';
        const imgTag = link.querySelector('img') || (container ? container.querySelector('img') : null);
        if (imgTag && imgTag.getAttribute('src')) imageUrl = imgTag.getAttribute('src');

        const priceMatch = containerText.match(priceRegex);
        const priceText = priceMatch ? priceMatch[0] : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def extract_bestbuy_products(page) -> List[Dict[str, Any]]:
    extraction_script = r"""
    () => {
      const items = Array.from(document.querySelectorAll('.sku-item'));
      const results = [];
      const seen = new Set();

      for (const item of items) {
        const title = item.querySelector('.sku-title a');
        const price = item.querySelector('.priceView-hero-price span, .priceView-customer-price span');
        if (!title) continue;

        const href = title.getAttribute('href') || '';
        const name = (title.innerText || '').trim();
        const priceText = (price ? price.innerText : '').trim();
        const image = item.querySelector('img');
        const imageUrl = image ? (image.getAttribute('src') || '') : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def extract_slickdeals_products(page) -> List[Dict[str, Any]]:
    extraction_script = r"""
    () => {
      const items = Array.from(
        document.querySelectorAll(
          '.dealCard, .resultRow, .dp-p, .searchResult, [data-threadid], [data-id]'
        )
      );
      const results = [];
      const seen = new Set();

      for (const item of items) {
        const title = item.querySelector(
          '.dealTitle, .dealTitle a, a.dealTitle, a[data-did], a[href*="/f/"], a[href*="/deal/"]'
        );
        const price = item.querySelector('.dealPrice, .price, .dealCard-price, [data-price]');
        const link = title && title.tagName.toLowerCase() === 'a' ? title : (title ? title.querySelector('a') : null);
        if (!link) continue;

        const href = link.getAttribute('href') || '';
        const name = (link.innerText || '').trim();
        const priceText = (price ? (price.innerText || price.getAttribute('data-price') || '') : '').trim();
        const image = item.querySelector('img');
        const imageUrl = image ? (image.getAttribute('src') || '') : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      if (results.length) {
        return results;
      }

      const priceRegex = /\$\s*\d[\d,]*(?:\.\d{2})?/;
      const links = Array.from(document.querySelectorAll('a[href*="/f/"], a[href*="/deal/"]'));

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
        let imageUrl = '';
        const imgTag = link.querySelector('img') || (container ? container.querySelector('img') : null);
        if (imgTag && imgTag.getAttribute('src')) imageUrl = imgTag.getAttribute('src');

        const priceMatch = containerText.match(priceRegex);
        const priceText = priceMatch ? priceMatch[0] : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def extract_pawnamerica_products(page) -> List[Dict[str, Any]]:
    extraction_script = r"""
    () => {
      const cards = Array.from(document.querySelectorAll('.ps-product'));
      const results = [];
      const seen = new Set();

      for (const card of cards) {
        const title = card.querySelector('.ps-product__title');
        const price = card.querySelector('.ps-product__price');
        const link = title || card.querySelector('.ps-product__thumbnail a[href]');

        if (!link) continue;
        const href = link.getAttribute('href') || '';
        const name = (title ? title.innerText : link.innerText || '').trim();
        const priceText = (price ? price.innerText : '').trim();
        const image = card.querySelector('img');
        const imageUrl = image ? (image.getAttribute('src') || '') : '';

        const key = href + '|' + name + '|' + priceText;
        if (seen.has(key)) continue;
        seen.add(key);

        results.push({ href, name, priceText, imageUrl });
      }

      return results;
    }
    """
    return page.evaluate(extraction_script)


def coerce_products(
    raw_items: Iterable[Dict[str, Any]],
    *,
    base_url: str,
    source: str,
    max_items: int,
    query: str,
    provider: Dict[str, Any],
) -> List[Product]:
    products: List[Product] = []
    seen_urls = set()
    tokens = query_tokens(query)

    for item in raw_items:
        href = (item.get("href") or "").strip()
        if not href:
            continue

        full_url = normalize_url(href, base_url)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        if not is_product_url(full_url, provider):
            continue

        name = (item.get("name") or "").strip()
        price_text = (item.get("priceText") or "").strip()
        image_url = (item.get("imageUrl") or "").strip() or None
        price = parse_price_to_float(price_text) if price_text else None

        if not name:
            continue
        if not is_relevant_name(name, tokens):
            continue
        if price is not None and price <= 0:
            continue

        products.append(Product(
            name=name,
            price=price,
            url=full_url,
            source=source,
            image_url=image_url,
        ))
        if len(products) >= max_items:
            break

    return products


def scrape_provider_page(
    page,
    provider: Dict[str, Any],
    query: str,
    *,
    max_items: int,
    include_auctions: bool,
) -> List[Product]:
    base_url = provider["base_url"]
    search_url = provider["search_url"].format(query=quote_plus(query))
    if provider.get("id") == "ebay" and not include_auctions:
        search_url = f"{search_url}&LH_BIN=1&LH_Auction=0"

    page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    wait_for_selector = provider.get("wait_for_selector")
    if wait_for_selector:
        try:
            page.wait_for_selector(wait_for_selector, timeout=WAIT_FOR_SELECTOR_TIMEOUT_MS)
        except TimeoutError:
            pass
    page.wait_for_timeout(provider.get("settle_ms", DEFAULT_SETTLE_MS))

    provider_id = provider.get("id")
    if provider_id == "pawnamerica":
        raw_items = extract_pawnamerica_products(page)
    elif provider_id == "newegg":
        raw_items = extract_newegg_products(page)
    elif provider_id == "walmart":
        raw_items = extract_walmart_products(page)
    elif provider_id == "bestbuy":
        raw_items = extract_bestbuy_products(page)
    elif provider_id == "slickdeals":
        raw_items = extract_slickdeals_products(page)
    else:
        raw_items = extract_products_from_dom(page)
    if not raw_items:
        page.wait_for_timeout(1400)
        if provider_id == "pawnamerica":
            raw_items = extract_pawnamerica_products(page)
        elif provider_id == "newegg":
            raw_items = extract_newegg_products(page)
        elif provider_id == "walmart":
            raw_items = extract_walmart_products(page)
        elif provider_id == "bestbuy":
            raw_items = extract_bestbuy_products(page)
        elif provider_id == "slickdeals":
            raw_items = extract_slickdeals_products(page)
        else:
            raw_items = extract_products_from_dom(page)
    return coerce_products(
        raw_items,
        base_url=base_url,
        source=provider["name"],
        max_items=max_items,
        query=query,
        provider=provider,
    )


def scrape_provider_standalone(
    provider: Dict[str, Any],
    query: str,
    *,
    max_items: int,
    include_auctions: bool,
) -> tuple[Dict[str, Any], List[Product], str]:
    status = "ok"
    products: List[Product] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        try:
            products = scrape_provider_page(
                page,
                provider,
                query,
                max_items=max_items,
                include_auctions=include_auctions,
            )
        except TimeoutError:
            status = "timeout"
            logger.warning("provider timeout: %s", provider.get("name"))
        except Exception:
            status = "error"
            logger.exception("provider error: %s", provider.get("name"))
        finally:
            page.close()
            context.close()
            browser.close()
    return provider, products, status


def sort_products(products: List[Product], query: str, sort_by: str) -> List[Product]:
    if sort_by == "price_high":
        return sorted(
            products,
            key=lambda p: (p.price is None, -(p.price or 0)),
        )
    if sort_by == "price_low":
        return sorted(
            products,
            key=lambda p: (p.price is None, p.price if p.price is not None else 10**12),
        )
    if sort_by == "ending_soon":
        return sorted(
            products,
            key=lambda p: (p.auction_end is None, p.auction_end or 10**18),
        )
    tokens = query_tokens(query)
    def sort_key(product: Product) -> tuple:
        exact_phrase, match_count, missing, boost, penalty = relevance_sort_key(
            product.name,
            tokens,
            query,
        )
        return (
            -exact_phrase,
            -match_count,
            missing,
            -boost,
            penalty,
            product.price is None,
            product.price or 10**12,
        )
    return sorted(
        products,
        key=sort_key,
    )


def scrape_all_providers(
    query: str,
    *,
    max_items_per_site: int = 35,
    include_auctions: bool = True,
    sort_by: str = "relevance",
) -> List[Product]:
    all_products: List[Product] = []
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROVIDERS) as executor:
        futures = [
            executor.submit(
                scrape_provider_standalone,
                provider,
                query,
                max_items=max_items_per_site,
                include_auctions=include_auctions,
            )
            for provider in SEARCH_PROVIDERS
        ]
        for future in as_completed(futures):
            _, products, _ = future.result()
            all_products.extend(products)

    elapsed = time.perf_counter() - started
    logger.info(
        "scraped %s products in %.2fs across %s sites",
        len(all_products),
        elapsed,
        len(SEARCH_PROVIDERS),
    )
    return sort_products(all_products, query, sort_by)


def stream_scrape_events(query: str, *, max_items_per_site: int, include_auctions: bool, sort_by: str):
    total = len(SEARCH_PROVIDERS)
    all_products: List[Product] = []
    started = time.perf_counter()

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROVIDERS) as executor:
        futures = [
            executor.submit(
                scrape_provider_standalone,
                provider,
                query,
                max_items=max_items_per_site,
                include_auctions=include_auctions,
            )
            for provider in SEARCH_PROVIDERS
        ]
        for future in as_completed(futures):
            provider, products, status = future.result()
            all_products.extend(products)
            completed += 1
            yield {
                "type": "progress",
                "provider": provider["name"],
                "provider_id": provider["id"],
                "completed": completed,
                "total": total,
                "status": status,
                "found": len(products),
            }

    elapsed = time.perf_counter() - started
    all_products = sort_products(all_products, query, sort_by)
    yield {
        "type": "done",
        "elapsed": round(elapsed, 2),
        "results": [asdict(p) for p in all_products],
    }


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
    query = normalize_query(request.args.get("q") or "")
    max_items = parse_int_param(
        request.args.get("limit"),
        MAX_ITEMS_PER_SITE_DEFAULT,
        min_value=5,
        max_value=120,
    )
    include_auctions = parse_bool_flag(request.args.get("auctions"), True)
    sort_by = (request.args.get("sort") or "relevance").strip()
    if sort_by not in ALLOWED_SORTS:
        sort_by = "relevance"

    products: List[Product] = []
    grouped: Dict[str, List[Product]] = {}
    if query:
        if is_rate_limited(client_ip()):
            return render_template(
                "index.html",
                query=query,
                products=[],
                grouped={},
                providers=SEARCH_PROVIDERS,
                error_message="Too many requests. Please wait a moment and try again.",
            ), 429
        products = scrape_all_providers(
            query,
            max_items_per_site=max_items,
            include_auctions=include_auctions,
            sort_by=sort_by,
        )
        grouped = group_by_source(products)

    return render_template(
        "index.html",
        query=query,
        products=products,
        grouped=grouped,
        providers=SEARCH_PROVIDERS,
        error_message=None,
    )


@app.route("/api/search")
def api_search():
    query = normalize_query(request.args.get("q") or "")
    max_items = parse_int_param(
        request.args.get("limit"),
        MAX_ITEMS_PER_SITE_DEFAULT,
        min_value=5,
        max_value=120,
    )
    include_auctions = parse_bool_flag(request.args.get("auctions"), True)
    sort_by = (request.args.get("sort") or "relevance").strip()
    if sort_by not in ALLOWED_SORTS:
        sort_by = "relevance"

    if not query:
        return jsonify({"error": "Query is required"}), 400
    if is_rate_limited(client_ip()):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429

    products = scrape_all_providers(
        query,
        max_items_per_site=max_items,
        include_auctions=include_auctions,
        sort_by=sort_by,
    )
    return jsonify({"results": [asdict(p) for p in products]})


@app.route("/api/search/stream")
def api_search_stream():
    query = normalize_query(request.args.get("q") or "")
    max_items = parse_int_param(
        request.args.get("limit"),
        MAX_ITEMS_PER_SITE_DEFAULT,
        min_value=5,
        max_value=120,
    )
    include_auctions = parse_bool_flag(request.args.get("auctions"), True)
    sort_by = (request.args.get("sort") or "relevance").strip()
    if sort_by not in ALLOWED_SORTS:
        sort_by = "relevance"

    if not query:
        return jsonify({"error": "Query is required"}), 400
    if is_rate_limited(client_ip()):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429

    def event_stream():
        for event in stream_scrape_events(
            query,
            max_items_per_site=max_items,
            include_auctions=include_auctions,
            sort_by=sort_by,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - START_TIME),
    }


@app.route("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Disallow: /api/search",
        "Disallow: /api/search/stream",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = env_int("PORT", 5000, min_value=1, max_value=65535)
    debug = os.getenv("APP_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
