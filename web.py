import logging
import os
from contextlib import asynccontextmanager
from html import escape

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update

from bot import build_application, post_init, post_shutdown

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app = build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    await post_init(telegram_app)
    app.state.telegram_app = telegram_app
    app.state.tmdb_api_key = os.getenv("TMDB_API_KEY")
    logger.info("Application startup complete")

    try:
        yield
    finally:
        await post_shutdown(telegram_app)
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Application shutdown complete")


app = FastAPI(title="MovieMax Bot + Player", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    telegram_app = request.app.state.telegram_app
    data = await request.json()
    try:
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception:
        logger.exception("Failed to process webhook update")
        return JSONResponse(status_code=400, content={"ok": False})
    return JSONResponse(status_code=200, content={"ok": True})


@app.get("/player", response_class=HTMLResponse)
async def player(request: Request, tmdb_id: int, type: str, s: int | None = None, e: int | None = None):
    media_type = type.lower()
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=400, detail="type must be movie or tv")

    tmdb_api_key = request.app.state.tmdb_api_key
    if not tmdb_api_key:
        raise HTTPException(status_code=500, detail="TMDB_API_KEY not configured")

    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
    params = {"api_key": tmdb_api_key}

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        status = 404 if exc.response.status_code == 404 else 502
        raise HTTPException(status_code=status, detail="Failed to fetch media details") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="TMDB request error") from exc

    title = escape(data.get("title") or data.get("name") or "Untitled")
    poster_path = data.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
    rating = data.get("vote_average")
    rating_text = f"{rating:.1f}/10" if isinstance(rating, (int, float)) else "N/A"
    release = escape(data.get("release_date") or data.get("first_air_date") or "Unknown")
    overview = escape(data.get("overview") or "No overview available.")

    iframe_src = f"https://screenscape.me/embed?tmdb={tmdb_id}&type={media_type}"
    if media_type == "tv":
        season_num = s or 1
        episode_num = e or 1
        iframe_src += f"&s={season_num}&e={episode_num}"

    html = f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{title} • MovieMax Player</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{
      margin: 0;
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: #0f1014;
      color: #f4f4f5;
      line-height: 1.5;
    }}
    .container {{
      max-width: 1000px;
      margin: 0 auto;
      padding: 20px;
      display: grid;
      gap: 20px;
    }}
    .card {{
      background: #171923;
      border: 1px solid #26293a;
      border-radius: 14px;
      padding: 16px;
    }}
    .meta {{
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 16px;
      align-items: start;
    }}
    .poster {{
      width: 100%;
      border-radius: 12px;
      border: 1px solid #2f3348;
      background: #111;
    }}
    .chips {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 8px 0 12px; }}
    .chip {{
      font-size: 0.9rem;
      border: 1px solid #3b3f56;
      border-radius: 999px;
      padding: 4px 10px;
      color: #d4d4d8;
    }}
    h1 {{ margin: 0 0 8px; font-size: 1.8rem; }}
    p {{ margin: 0; color: #e5e7eb; }}
    @media (max-width: 700px) {{
      .meta {{ grid-template-columns: 1fr; }}
      .poster {{ max-width: 240px; }}
    }}
  </style>
</head>
<body>
  <main class=\"container\">
    <section class=\"card meta\">
      <div>
        {f'<img class="poster" src="{poster_url}" alt="{title} poster" />' if poster_url else '<div class="poster" style="aspect-ratio:2/3;display:grid;place-items:center;">No poster</div>'}
      </div>
      <div>
        <h1>{title}</h1>
        <div class=\"chips\">
          <span class=\"chip\">Type: {media_type.upper()}</span>
          <span class=\"chip\">Rating: {rating_text}</span>
          <span class=\"chip\">Release: {release}</span>
        </div>
        <p>{overview}</p>
      </div>
    </section>

    <section class=\"card\">
      <div style=\"width: 100%; max-width: 1000px; margin: 0 auto; aspect-ratio: 16/9;\">
        <iframe
          src=\"{iframe_src}\"
          width=\"100%\"
          height=\"100%\"
          frameborder=\"0\"
          allowfullscreen
          allow=\"autoplay; fullscreen; picture-in-picture\"
          style=\"border: none; border-radius: 12px; width: 100%; height: 100%;\">
        </iframe>
      </div>
    </section>
  </main>
</body>
</html>
"""
    return HTMLResponse(content=html)