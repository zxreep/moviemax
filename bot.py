import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


@dataclass
class MediaItem:
    tmdb_id: int
    media_type: str
    title: str
    overview: str
    vote_average: float | None
    release_date: str | None
    poster_path: str | None


class TMDBClient:
    def __init__(self, api_key: str, timeout: float = 12.0) -> None:
        self.api_key = api_key
        self.base_url = "https://api.themoviedb.org/3"
        self.image_base_url = "https://image.tmdb.org/t/p/w500"
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = {"api_key": self.api_key, **params}
        response = await self.client.get(f"{self.base_url}{path}", params=query)
        response.raise_for_status()
        return response.json()

    async def multi_search(self, query: str, page: int = 1) -> list[MediaItem]:
        data = await self._get("/search/multi", query=query, page=page, include_adult=False)
        return self._parse_media_items(data.get("results", []))

    async def trending_movies(self, page: int = 1) -> list[MediaItem]:
        data = await self._get("/trending/movie/week", page=page)
        return self._parse_media_items(data.get("results", []), forced_type="movie")

    async def popular_movies(self, page: int = 1) -> list[MediaItem]:
        data = await self._get("/movie/popular", page=page)
        return self._parse_media_items(data.get("results", []), forced_type="movie")

    async def get_details(self, media_type: str, tmdb_id: int) -> MediaItem:
        path = f"/{media_type}/{tmdb_id}"
        data = await self._get(path)
        return self._to_media_item(data, forced_type=media_type)

    def _parse_media_items(self, raw_items: list[dict[str, Any]], forced_type: str | None = None) -> list[MediaItem]:
        parsed: list[MediaItem] = []
        for item in raw_items:
            media_type = forced_type or item.get("media_type")
            if media_type not in {"movie", "tv"}:
                continue
            parsed.append(self._to_media_item(item, forced_type=media_type))
            if len(parsed) == 10:
                break
        return parsed

    def _to_media_item(self, data: dict[str, Any], forced_type: str) -> MediaItem:
        title = data.get("title") or data.get("name") or "Untitled"
        release_date = data.get("release_date") or data.get("first_air_date")
        return MediaItem(
            tmdb_id=int(data["id"]),
            media_type=forced_type,
            title=title,
            overview=data.get("overview") or "No overview available.",
            vote_average=data.get("vote_average"),
            release_date=release_date,
            poster_path=data.get("poster_path"),
        )


def _media_emoji(media_type: str) -> str:
    return "🎬" if media_type == "movie" else "📺"


def _format_item_line(item: MediaItem) -> str:
    rating = f"⭐ {item.vote_average:.1f}" if item.vote_average is not None else "⭐ N/A"
    date = item.release_date or "Unknown date"
    return f"{_media_emoji(item.media_type)} {item.title} ({date}) • {rating}"


def _details_caption(item: MediaItem) -> str:
    rating = f"{item.vote_average:.1f}/10" if item.vote_average is not None else "N/A"
    date = item.release_date or "Unknown"
    return (
        f"{_media_emoji(item.media_type)} <b>{item.title}</b>\n"
        f"⭐ <b>Rating:</b> {rating}\n"
        f"📅 <b>Release:</b> {date}\n\n"
        f"📝 <b>Overview</b>\n{item.overview}"
    )


def _player_url(base_url: str, item: MediaItem) -> str:
    """Build a stable player URL even if APP_BASE_URL is misconfigured.

    If APP_BASE_URL is provided as a webhook URL (e.g. .../webhook),
    this normalizes it back to the site root before appending /player.
    """
    split = urlsplit(base_url.strip())
    clean_path = (split.path or "").rstrip("/")
    if clean_path.endswith("/webhook"):
        clean_path = clean_path[: -len("/webhook")]

    normalized_base = urlunsplit((split.scheme, split.netloc, clean_path, "", "")).rstrip("/")
    return f"{normalized_base}/player?tmdb_id={item.tmdb_id}&type={item.media_type}"


def _render_results_text(source: str, items: list[MediaItem], page: int, query: str | None = None) -> str:
    if source == "search":
        header = f"🔎 Results for: <b>{query}</b>"
    elif source == "trending":
        header = "🔥 <b>Trending Movies</b>"
    else:
        header = "🍿 <b>Popular Movies</b>"

    if not items:
        return f"{header}\n\nNo results found on page {page}."

    lines = "\n".join(f"{idx + 1}. {_format_item_line(item)}" for idx, item in enumerate(items))
    return f"{header}\nPage {page}\n\n{lines}\n\nTap a title button below."


def _build_results_keyboard(source: str, items: list[MediaItem], page: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for item in items:
        text = f"{_media_emoji(item.media_type)} {item.title[:48]}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"item:{item.media_type}:{item.tmdb_id}")])

    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav:{source}:{page - 1}"))
    nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"nav:{source}:{page + 1}"))
    buttons.append(nav_row)
    return InlineKeyboardMarkup(buttons)


def _details_keyboard(item: MediaItem, app_base_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Play", url=_player_url(app_base_url, item))],
            [InlineKeyboardButton("🔙 Back", callback_data="back")],
        ]
    )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Welcome!\n"
        "I can help you discover movies and TV shows with rich details.\n"
        "Send me any movie or TV show name to search 🎬"
    )


async def _show_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source: str,
    page: int,
    query: str | None = None,
    edit: bool = False,
) -> None:
    tmdb: TMDBClient = context.application.bot_data["tmdb_client"]

    try:
        if source == "search":
            items = await tmdb.multi_search(query=query or "", page=page)
        elif source == "trending":
            items = await tmdb.trending_movies(page=page)
        else:
            items = await tmdb.popular_movies(page=page)
    except httpx.HTTPError:
        logger.exception("TMDB API request failed")
        text = "⚠️ TMDB is currently unavailable. Please try again in a moment."
        if update.callback_query:
            await update.callback_query.answer("TMDB error", show_alert=True)
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    context.user_data["last_list"] = {"source": source, "page": page, "query": query}
    context.user_data["last_items"] = [item.__dict__ for item in items]

    text = _render_results_text(source=source, items=items, page=page, query=query)
    keyboard = _build_results_keyboard(source=source, items=items, page=page)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif update.message:
        await update.message.reply_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def search_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("Please send a movie or TV show name.")
        return
    await _show_results(update, context, source="search", page=1, query=query)


async def trending_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_results(update, context, source="trending", page=1)


async def popular_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_results(update, context, source="popular", page=1)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "back":
        try:
            await query.message.delete()
        except BadRequest:
            await query.answer("Cannot go back from here.", show_alert=True)
        return

    if data.startswith("nav:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid pagination action.", show_alert=True)
            return
        _, source, page_s = parts
        try:
            page = max(1, int(page_s))
        except ValueError:
            await query.answer("Invalid page value.", show_alert=True)
            return
        saved = context.user_data.get("last_list", {})
        saved_query = saved.get("query") if source == "search" else None
        await _show_results(update, context, source=source, page=page, query=saved_query, edit=True)
        return

    if data.startswith("item:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid item action.", show_alert=True)
            return
        _, media_type, tmdb_id_s = parts
        if media_type not in {"movie", "tv"}:
            await query.answer("Unsupported media type.", show_alert=True)
            return
        try:
            tmdb_id = int(tmdb_id_s)
        except ValueError:
            await query.answer("Invalid media id.", show_alert=True)
            return

        tmdb: TMDBClient = context.application.bot_data["tmdb_client"]
        try:
            item = await tmdb.get_details(media_type=media_type, tmdb_id=tmdb_id)
        except httpx.HTTPError:
            logger.exception("Failed to fetch item details")
            await query.answer("Could not fetch details right now.", show_alert=True)
            return

        app_base_url = context.application.bot_data["app_base_url"]
        caption = _details_caption(item)
        keyboard = _details_keyboard(item, app_base_url=app_base_url)

        poster_url = f"{tmdb.image_base_url}{item.poster_path}" if item.poster_path else None
        if poster_url:
            await query.message.reply_photo(
                photo=poster_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await query.message.reply_text(
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        return

    await query.answer("Unknown action.", show_alert=True)


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = (update.inline_query.query or "").strip()
    if not inline_query:
        return

    tmdb: TMDBClient = context.application.bot_data["tmdb_client"]
    app_base_url: str = context.application.bot_data["app_base_url"]
    try:
        items = await tmdb.multi_search(inline_query, page=1)
    except httpx.HTTPError:
        logger.exception("Inline TMDB search failed")
        return

    results: list[InlineQueryResultArticle] = []
    for item in items[:10]:
        description = f"{_media_emoji(item.media_type)} {item.release_date or 'Unknown'} • ⭐ {item.vote_average or 'N/A'}"
        play_url = _player_url(app_base_url, item)
        content = InputTextMessageContent(
            message_text=(
                f"{_details_caption(item)}\n\n"
                f"▶️ Watch: {play_url}"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        results.append(
            InlineQueryResultArticle(
                id=f"{item.media_type}-{item.tmdb_id}",
                title=f"{_media_emoji(item.media_type)} {item.title}",
                description=description,
                input_message_content=content,
            )
        )

    await update.inline_query.answer(results=results, cache_time=60)


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    tmdb_api_key = os.getenv("TMDB_API_KEY")
    app_base_url = os.getenv("APP_BASE_URL")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not tmdb_api_key:
        raise RuntimeError("TMDB_API_KEY is required")
    if not app_base_url:
        raise RuntimeError("APP_BASE_URL is required (e.g., https://your-app.onrender.com)")

    application = Application.builder().token(token).build()
    application.bot_data["tmdb_client"] = TMDBClient(tmdb_api_key)
    application.bot_data["app_base_url"] = app_base_url

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("trending", trending_handler))
    application.add_handler(CommandHandler("popular", popular_handler))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text_handler))

    return application


async def post_init(application: Application) -> None:
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await application.bot.set_webhook(webhook_url)
        logger.info("Webhook set to %s", webhook_url)


async def post_shutdown(application: Application) -> None:
    tmdb: TMDBClient = application.bot_data.get("tmdb_client")
    if tmdb:
        await tmdb.close()
  
