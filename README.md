# Deal Finder

Fast electronics deal search across major retailers and marketplaces with a clean UI, streaming progress, and relevance-first sorting.

## Features
- Scrapes multiple providers in parallel and streams progress updates
- Relevance-aware ranking that prioritizes consoles over accessories
- Price sorting, auction filtering, and provider breakdowns
- Basic rate limiting, security headers, and health endpoint

## Quickstart
1) Create a virtual environment and install dependencies:
```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows PowerShell
pip install -r requirements.txt
python -m playwright install chromium
```

2) Run the app:
```bash
python python/main.py
```

Open `http://localhost:5000`.

## Configuration
Set environment variables as needed:
- `APP_HOST` (default `0.0.0.0`)
- `PORT` (default `5000`)
- `APP_DEBUG` (`1` enables debug)
- `LOG_LEVEL` (default `INFO`)
- `MAX_CONCURRENT_PROVIDERS` (default `6`)
- `MAX_ITEMS_PER_SITE` (default `35`)
- `DEFAULT_SETTLE_MS` (default `1600`)
- `NAV_TIMEOUT_MS` (default `35000`)
- `WAIT_FOR_SELECTOR_TIMEOUT_MS` (default `12000`)
- `PLAYWRIGHT_HEADLESS` (`0` to show browser)
- `RATE_LIMIT_PER_MINUTE` (default `30`)
- `MAX_QUERY_LENGTH` (default `120`)

## Docker
Build and run:
```bash
docker build -t dealfinder .
docker run -p 5000:5000 -e PORT=5000 dealfinder
```

## Production notes
- Scraping is best-effort; some providers block automation or change markup.
- Respect each site's terms and robots policies.
- Consider caching, provider health checks, and per-provider backoff for scale.

## Endpoints
- `/` UI
- `/api/search` JSON
- `/api/search/stream` Server-sent events
- `/health` health status
