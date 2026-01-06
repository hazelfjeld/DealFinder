import argparse
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from main import SEARCH_PROVIDERS, scrape_provider_standalone


def run_checks(query: str, max_items: int, include_auctions: bool, workers: int):
    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                scrape_provider_standalone,
                provider,
                query,
                max_items=max_items,
                include_auctions=include_auctions,
            )
            for provider in SEARCH_PROVIDERS
        ]
        for future in as_completed(futures):
            provider, products, status = future.result()
            results.append({
                "provider_id": provider["id"],
                "provider_name": provider["name"],
                "status": status,
                "count": len(products),
                "sample_url": products[0].url if products else "",
            })
    elapsed = time.perf_counter() - started
    return results, elapsed


def write_outputs(results, elapsed, json_path: str, csv_path: str):
    payload = {
        "elapsed_seconds": round(elapsed, 2),
        "providers": results,
    }
    with open(json_path, "w", encoding="utf-8") as json_handle:
        json.dump(payload, json_handle, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8") as csv_handle:
        writer = csv.writer(csv_handle)
        writer.writerow(["provider_id", "provider_name", "status", "count", "sample_url"])
        for row in sorted(results, key=lambda item: (item["count"], item["provider_name"].lower())):
            writer.writerow([row["provider_id"], row["provider_name"], row["status"], row["count"], row["sample_url"]])


def main():
    parser = argparse.ArgumentParser(description="QA check for provider scraping results.")
    parser.add_argument("--query", required=True, help="Search query to test")
    parser.add_argument("--limit", type=int, default=25, help="Max items per site")
    parser.add_argument("--no-auctions", action="store_true", help="Exclude auctions where supported")
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers")
    parser.add_argument("--json", default="qa_report.json", help="Output JSON path")
    parser.add_argument("--csv", default="qa_report.csv", help="Output CSV path")
    args = parser.parse_args()

    results, elapsed = run_checks(
        args.query,
        args.limit,
        include_auctions=not args.no_auctions,
        workers=args.workers,
    )
    write_outputs(results, elapsed, args.json, args.csv)
    empty = [row for row in results if row["count"] == 0]
    print(f"[qa] {len(results)} providers checked in {elapsed:.2f}s")
    print(f"[qa] {len(empty)} providers returned 0 results")
    for row in sorted(empty, key=lambda item: item["provider_name"].lower()):
        print(f" - {row['provider_name']} ({row['status']})")


if __name__ == "__main__":
    main()
