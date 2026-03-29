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


@dataclass
class TVSeason:
    season_number: int
    name: str
    episode_count: int


@dataclass
class TVEpisode:
    episode_number: int
    name: str
    overview: str
    air_date: str | None
    vote_average: float | None


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

    async def get_tv_seasons(self, tv_id: int) -> list[TVSeason]:
        data = await self._get(f"/tv/{tv_id}")
        seasons: list[TVSeason] = []
        for season in data.get("seasons", []):
            season_number = season.get("season_number")
            # Season 0 is usually specials; skip for cleaner UX.
            if not isinstance(season_number, int) or season_number <= 0:
                continue
            seasons.append(
                TVSeason(
                    season_number=season_number,
                    name=season.get("name") or f"Season {season_number}",
                    episode_count=int(season.get("episode_count") or 0),
                )
            )
        return seasons

    async def get_tv_episodes(self, tv_id: int, season_number: int) -> list[TVEpisode]:
        data = await self._get(f"/tv/{tv_id}/season/{season_number}")
        episodes: list[TVEpisode] = []
        for ep in data.get("episodes", []):
            episode_number = ep.get("episode_number")
            if not isinstance(episode_number, int):
                continue
            episodes.append(
                TVEpisode(
                    episode_number=episode_number,
                    name=ep.get("name") or f"Episode {episode_number}",
                    overview=ep.get("overview") or "No overview available.",
                    air_date=ep.get("air_date"),
                    vote_average=ep.get("vote_average"),
                )
            )
        return episodes

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


def _player_url(base_url: str, item: MediaItem, season: int | None = None, episode: int | None = None) -> str:
    """Build a stable player URL even if APP_BASE_URL is misconfigured.

    If APP_BASE_URL is provided as a webhook URL (e.g. .../webhook),
    this normalizes it back to the site root before appending /player.
    """
    split = urlsplit(base_url.strip())
    clean_path = (split.path or "").rstrip("/")
    if clean_path.endswith("/webhook"):
        clean_path = clean_path[: -len("/webhook")]

    normalized_base = urlunsplit((split.scheme, split.netloc, clean_path, "", "")).rstrip("/")
    url = f"{normalized_base}/player?tmdb_id={item.tmdb_id}&type={item.media_type}"
    if item.media_type == "tv":
        s = season or 1
        e = episode or 1
        url += f"&s={s}&e={e}"
    return url


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
    play_url = _player_url(app_base_url, item)
    rows = [[InlineKeyboardButton("▶️ Play", url=play_url)]]
    if item.media_type == "tv":
        rows.append([InlineKeyboardButton("📚 Seasons & Episodes", callback_data=f"tvseasons:{item.tmdb_id}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    return InlineKeyboardMarkup(
        rows
    )


def _tv_seasons_keyboard(tv_id: int, seasons: list[TVSeason]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for season in seasons:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📀 S{season.season_number} ({season.episode_count} eps)",
                    callback_data=f"tvseason:{tv_id}:{season.season_number}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔙 Close", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def _tv_episodes_keyboard(tv_id: int, season_number: int, episodes: list[TVEpisode]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for ep in episodes:
        row.append(
            InlineKeyboardButton(
                text=f"E{ep.episode_number}",
                callback_data=f"tvep:{tv_id}:{season_number}:{ep.episode_number}",
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Back to Seasons", callback_data=f"tvseasons:{tv_id}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="back")])
    return InlineKeyboardMarkup(rows)


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

    if data.startswith("tvseasons:"):
        parts = data.split(":")
        if len(parts) != 2:
            await query.answer("Invalid TV season action.", show_alert=True)
            return
        try:
            tv_id = int(parts[1])
        except ValueError:
            await query.answer("Invalid TV id.", show_alert=True)
            return

        tmdb: TMDBClient = context.application.bot_data["tmdb_client"]
        try:
            seasons = await tmdb.get_tv_seasons(tv_id)
        except httpx.HTTPError:
            logger.exception("Failed to fetch TV seasons")
            await query.answer("Could not fetch seasons right now.", show_alert=True)
            return

        if not seasons:
            await query.message.reply_text("No seasons found for this TV show.")
            return

        lines = [f"📺 <b>Select Season</b> (TMDB ID: <code>{tv_id}</code>)"]
        for season in seasons:
            lines.append(f"• S{season.season_number} — {season.name} ({season.episode_count} episodes)")
        await query.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=_tv_seasons_keyboard(tv_id, seasons),
        )
        return

    if data.startswith("tvseason:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Invalid season callback.", show_alert=True)
            return
        try:
            tv_id = int(parts[1])
            season_number = int(parts[2])
        except ValueError:
            await query.answer("Invalid season parameters.", show_alert=True)
            return

        tmdb: TMDBClient = context.application.bot_data["tmdb_client"]
        try:
            episodes = await tmdb.get_tv_episodes(tv_id=tv_id, season_number=season_number)
        except httpx.HTTPError:
            logger.exception("Failed to fetch TV episodes")
            await query.answer("Could not fetch episodes right now.", show_alert=True)
            return

        if not episodes:
            await query.message.reply_text(f"No episodes found for Season {season_number}.")
            return

        await query.message.reply_text(
            text=(
                f"📺 <b>Season {season_number}</b> • TMDB ID: <code>{tv_id}</code>\n"
                f"Select an episode to generate a Play link with <b>type=tv&s={season_number}&e=...</b>."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_tv_episodes_keyboard(tv_id, season_number, episodes),
        )
        return

    if data.startswith("tvep:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.answer("Invalid episode callback.", show_alert=True)
            return
        try:
            tv_id = int(parts[1])
            season_number = int(parts[2])
            episode_number = int(parts[3])
        except ValueError:
            await query.answer("Invalid episode parameters.", show_alert=True)
            return

        tmdb: TMDBClient = context.application.bot_data["tmdb_client"]
        app_base_url = context.application.bot_data["app_base_url"]
        try:
            details = await tmdb.get_details(media_type="tv", tmdb_id=tv_id)
            episodes = await tmdb.get_tv_episodes(tv_id=tv_id, season_number=season_number)
        except httpx.HTTPError:
            logger.exception("Failed to fetch TV episode details")
            await query.answer("Could not fetch episode details right now.", show_alert=True)
            return

        selected = next((ep for ep in episodes if ep.episode_number == episode_number), None)
        if not selected:
            await query.answer("Episode not found.", show_alert=True)
            return

        rating = f"{selected.vote_average:.1f}/10" if selected.vote_average is not None else "N/A"
        play_url = _player_url(app_base_url, details, season=season_number, episode=episode_number)
        await query.message.reply_text(
            text=(
                f"📺 <b>{details.title}</b>\n"
                f"Season <b>{season_number}</b>, Episode <b>{episode_number}</b> — <b>{selected.name}</b>\n"
                f"⭐ Rating: <b>{rating}</b>\n"
                f"📅 Air date: <b>{selected.air_date or 'Unknown'}</b>\n\n"
                f"{selected.overview}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("▶️ Play Episode", url=play_url)],
                    [InlineKeyboardButton("🔙 Back to Episodes", callback_data=f"tvseason:{tv_id}:{season_number}")],
                    [InlineKeyboardButton("❌ Close", callback_data="back")],
                ]
            ),
            disable_web_page_preview=True,
        )
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


async def inline_query_handler(update, context) -> None:

    """Handle inline mode lookups (@bot query)."""
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
