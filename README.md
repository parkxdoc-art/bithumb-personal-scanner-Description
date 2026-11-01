# Bithumb KRW Signal Scanner (signal-only prototype)

## Included
- Bithumb public market/ticker/minute-candle API integration
- KRW markets ranked by 24h trading value
- Simple descending swing-high resistance estimate
- WATCH/BUY candidate classification, volume multiple setting, timeframe setting
- Signal-only: no exchange keys, no orders, no auto-trading

## Run locally
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:10000

## Deploy to Render
1. Upload these files to a GitHub repository.
2. In Render, choose New > Blueprint and connect the repository, or create a Python Web Service.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
5. No API keys are needed for the public endpoints used here.

## Strategy notes and limitations
- This is a first-pass heuristic, not a validated profitable strategy.
- The trendline is calculated from the last two local swing highs; it will not always match a human-drawn chart line.
- Current BUY label requires a descending resistance estimate, price holding above it, a bullish retest candle, and a relative-volume threshold. Treat as an alert candidate, not a recommendation.
- Stop/target values are calculated internally but the initial UI does not present them yet; extend the table/API before relying on them.
- The API is polled when Scan is clicked and optionally every 60 seconds; it is not a websocket stream.
- Public API limits and availability can change. Failed individual market requests are skipped.
- Backtest and paper-trading validation are required before any real-money use.
