# MovieMax Telegram Bot

Production-ready Telegram movie bot + web player using **python-telegram-bot (async v20+)**, **TMDB API**, and **FastAPI**.

## Features

 `/start` welcome flow.
 Text search via TMDB multi-search (movies + TV only).
 Inline keyboard result list (10 per page) with `Prev/Next` pagination.
 Rich media details view with poster, rating, release date, overview.
 Action buttons:
 - `▶️ Play` opens hosted player URL.
 - `🔙 Back` removes details message.
 - `/trending` command (trending movies).
 - `/popular` command (popular movies).
 - Inline mode support (`@your_bot query`) returning top 10 results.
 - Robust error handling for invalid callbacks, empty pages, and TMDB/API failures.
 - No DB: all short-lived state is kept in `context.user_data` only.

## Project Structure

 `bot.py` – telegram bot handlers, TMDB client, keyboard flows.
 `web.py` – FastAPI app for webhook + `/player` page.
 `requirements.txt` – dependencies.
 `Procfile` – Render start command.

## Environment Variables

Set these in Render:

- `TELEGRAM_BOT_TOKEN` – Telegram bot token from BotFather.
- `TMDB_API_KEY` – TMDB API key.
- `APP_BASE_URL` – your public app root URL (example: `https://moviemax.onrender.com`, **not** `/webhook`).
- `WEBHOOK_URL` – full webhook URL (example: `https://moviemax.onrender.com/webhook`)

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TMDB_API_KEY="..."
export APP_BASE_URL="http://localhost:8000"
export WEBHOOK_URL="http://localhost:8000/webhook"
uvicorn web:app --reload --host 0.0.0.0 --port 8000
```

## Render Deployment

1. Create a **Web Service** from this repo.
2. Runtime: Python.
3. Build command:
   ```bash
   pip install -r requirements.txt
   ```
4. Start command (already in `Procfile`):
   ```bash
   uvicorn web:app --host 0.0.0.0 --port $PORT
   ```
5. Add environment variables listed above.
6. Deploy.
7. Validate:
   - `GET /health` returns `{"status": "ok"}`.
   - Telegram webhook is set to your `/webhook` endpoint.

## Web Player Endpoint

`GET /player?tmdb_id=<id>&type=movie|tv[&s=<season>&e=<episode>]`

The page fetches TMDB details and renders:

- title
- poster
- rating
- release date
- overview
- embedded player iframe:

`https://screenscape.me/embed?tmdb=<tmdb_id>&type=<movie|tv>&s=<season>&e=<episode>` (for TV, defaults to `s=1&e=1` if omitted)


### Docker (Render Docker Service)

If you deploy as a Docker service, this repo now includes a `Dockerfile` that runs:

```bash
uvicorn web:app --host 0.0.0.0 --port $PORT
```

Set the same environment variables (`TELEGRAM_BOT_TOKEN`, `TMDB_API_KEY`, `APP_BASE_URL`, `WEBHOOK_URL`) in Render.

## Notes

- Designed for Render free tier and webhook-based delivery.
- No polling required in production.
- Stateless beyond `context.user_data`.
