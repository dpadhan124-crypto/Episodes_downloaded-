import asyncio
import logging
import os
import re
import shlex
import subprocess
import urllib.parse
from typing import List, Optional, Tuple

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://pocketfm.com",
    "Referer": "https://pocketfm.com/",
    "Content-Type": "application/json",
    "app-version": "180",
    "app-client": "consumer-web",
    "platform": "web",
    "app-name": "pocket_fm",
}

# Headers used by FFmpeg when fetching HLS segments
FFMPEG_HEADERS = (
    f"Referer: https://pocketfm.com/\r\n"
    f"User-Agent: {UA}\r\n"
)

NUM_KEYS = (
    "seq_number",
    "episode_number",
    "sequence_number",
    "index",
    "ep_no",
    "episode_no",
)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

# ---------------------------------------------------------------------------
# Pocket FM scraper (advanced)
# ---------------------------------------------------------------------------

async def _fetch_auth_token(client: httpx.AsyncClient) -> str:
    """Visit the search page to obtain an auth-token cookie."""
    await client.get("https://pocketfm.com/search", headers={"User-Agent": UA})
    token = urllib.parse.unquote(client.cookies.get("auth-token") or "").strip('"')
    if not token:
        logger.warning("No auth-token cookie found; proceeding without Authorization header.")
    return token


async def fetch_show_episodes(show_id: str) -> Tuple[str, Optional[int], List[dict]]:
    """
    Fetch all episodes for a Pocket FM show.

    Returns:
        (show_title, locking_point, list_of_episode_dicts)
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        token = await _fetch_auth_token(client)
        headers = {**BASE_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        episodes: List[dict] = []
        ptr = 0
        locking_point: Optional[int] = None
        show_title = "Unknown Show"

        for page in range(50):
            params = {
                "show_id": show_id,
                "curr_ptr": ptr,
                "info_level": "max",
            }

            # Retry loop for transient network errors
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await client.get(
                        "https://api.pocketfm.com/v2/content_api/show.get_details",
                        params=params,
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        break
                    logger.warning(
                        "Show details HTTP %s (attempt %s/%s)",
                        resp.status_code,
                        attempt,
                        MAX_RETRIES,
                    )
                except httpx.RequestError as exc:
                    logger.warning(
                        "Request error on page %s (attempt %s/%s): %s",
                        page,
                        attempt,
                        MAX_RETRIES,
                        exc,
                    )

                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)

            else:
                logger.error("Failed to fetch show details after %s retries.", MAX_RETRIES)
                break

            data = resp.json()
            if not data.get("result"):
                logger.info("No result on page %s; stopping.", page)
                break

            result = data["result"][0]

            if page == 0:
                show_title = result.get("show_title", "Unknown Show")
                locking_point = result.get("episode_locking_point", 0)

            page_episodes = result.get("stories", [])
            if not page_episodes:
                break

            episodes.extend(page_episodes)

            next_ptr = data.get("next_ptr", result.get("next_ptr"))
            if isinstance(next_ptr, int) and next_ptr > ptr:
                ptr = next_ptr
            else:
                ptr += len(page_episodes)

            if len(page_episodes) < 5:
                break

        return show_title, locking_point, episodes


def _episode_number(episode: dict, fallback: int) -> int:
    """Extract the episode number from whichever key is present."""
    for key in NUM_KEYS:
        value = episode.get(key)
        if str(value).isdigit():
            return int(value)
    return fallback


def _is_free(episode: dict, episode_number: int, locking_point: Optional[int]) -> bool:
    """Determine whether an episode is free."""
    if locking_point is not None and episode_number > locking_point:
        return False

    lock_flags = {
        key: episode[key]
        for key in (
            "is_locked",
            "locked",
            "is_premium",
            "is_paid",
            "is_coin_locked",
        )
        if key in episode
    }
    if any(lock_flags.values()):
        return False

    return True


def _extract_audio_url(episode: dict) -> Optional[str]:
    """Find the best audio URL in an episode payload."""
    # Prefer direct MP3
    for key in ("audio_url", "mp3_url", "file_url", "audio", "stream", "media"):
        value = episode.get(key)
        if isinstance(value, str) and value.startswith("http") and ".mp3" in value:
            return value

    # Check nested media_meta
    media_meta = episode.get("media_meta")
    if isinstance(media_meta, dict):
        value = media_meta.get("url")
        if isinstance(value, str) and value.startswith("http"):
            return value

    # Fall back to HLS / m3u8
    for key in ("hls", "stream", "audio_url", "file_url"):
        value = episode.get(key)
        if isinstance(value, str) and value.startswith("http") and ".m3u8" in value:
            return value

    # Last resort: scan all string values
    for value in episode.values():
        if isinstance(value, str) and value.startswith("http"):
            if ".mp3" in value or ".m3u8" in value:
                return value

    return None


# ---------------------------------------------------------------------------
# FFmpeg download / merge
# ---------------------------------------------------------------------------

def _run_ffmpeg(input_url: str, output_path: str) -> Tuple[bool, str]:
    """
    Use FFmpeg to download and merge an HLS playlist into a single MP3.

    Returns:
        (success, error_message)
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-headers",
        FFMPEG_HEADERS,
        "-i",
        input_url,
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ab",
        "192k",
        output_path,
    ]

    logger.info("Running FFmpeg: %s", " ".join(shlex.quote(c) for c in cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes per episode; adjust as needed
        )
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out after 600 seconds."
    except FileNotFoundError:
        return False, "FFmpeg executable not found. Please install FFmpeg."

    if result.returncode != 0:
        # Return the last few lines of stderr for diagnostics
        error_tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        return False, f"FFmpeg exited with {result.returncode}: {error_tail}"

    if not os.path.exists(output_path):
        return False, "FFmpeg reported success but output file is missing."

    return True, ""


async def download_episode_audio(audio_url: str, output_path: str) -> Tuple[bool, str]:
    """
    Download an episode's audio. Uses FFmpeg for HLS streams, direct HTTP for MP3.
    """
    if ".m3u8" in audio_url:
        # Run FFmpeg in a thread so we don't block the event loop
        return await asyncio.to_thread(_run_ffmpeg, audio_url, output_path)

    # Direct MP3 download
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", audio_url, headers={"User-Agent": UA}) as resp:
                resp.raise_for_status()
                with open(output_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to the Pocket FM Free Episode Downloader!\n\n"
        "Send me a Pocket FM **show link** or **show ID** and I will fetch "
        "all free episodes as MP3 files."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    # Extract a 40-char hex show ID from a URL, or use the raw text
    match = re.search(r"[a-f0-9]{40}", text, re.IGNORECASE)
    show_id = match.group(0) if match else text

    if len(show_id) < 10:
        await update.message.reply_text(
            "❌ Invalid show ID or link. Please send a valid Pocket FM link or ID."
        )
        return

    status_msg = await update.message.reply_text(
        "🔍 Fetching show details and checking free episodes…"
    )

    try:
        show_title, locking_point, all_episodes = await fetch_show_episodes(show_id)

        if not all_episodes:
            await status_msg.edit_text("❌ No episodes found for this show.")
            return

        # Filter free episodes
        free_episodes: List[Tuple[int, dict]] = []
        for idx, episode in enumerate(all_episodes, start=1):
            ep_num = _episode_number(episode, idx)
            if _is_free(episode, ep_num, locking_point):
                free_episodes.append((ep_num, episode))

        await status_msg.edit_text(
            f"📚 *{show_title}*\n"
            f"🔓 Locking point: Episode {locking_point}\n"
            f"📦 Total episodes: {len(all_episodes)} | Free: {len(free_episodes)}\n\n"
            f"📤 Starting upload of free episodes…",
            parse_mode="Markdown",
        )

        if not free_episodes:
            await update.message.reply_text("⚠️ No free episodes found for this show.")
            return

        for ep_num, episode in free_episodes:
            ep_title = (
                episode.get("story_title")
                or episode.get("title")
                or f"Episode {ep_num}"
            )

            audio_url = _extract_audio_url(episode)
            if not audio_url:
                logger.warning("Episode %s: no audio URL found, skipping.", ep_num)
                continue

            output_file = f"Ep_{ep_num}.mp3"

            try:
                success, error = await download_episode_audio(audio_url, output_file)
                if not success:
                    logger.error("Episode %s download failed: %s", ep_num, error)
                    continue

                file_size = os.path.getsize(output_file)
                if file_size < 50_000:  # sanity check: at least 50 KB
                    logger.warning(
                        "Episode %s output is suspiciously small (%s bytes), skipping.",
                        ep_num,
                        file_size,
                    )
                    continue

                with open(output_file, "rb") as audio_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        title=ep_title,
                        performer=show_title,
                        caption=f"🎧 Ep {ep_num}: {ep_title}",
                    )

            except Exception as exc:
                logger.exception("Episode %s failed: %s", ep_num, exc)
            finally:
                if os.path.exists(output_file):
                    os.remove(output_file)

            await asyncio.sleep(1)  # polite rate limiting

        await update.message.reply_text("✅ All free episodes have been sent!")

    except Exception as exc:
        logger.exception("Unhandled error while processing show %s", show_id)
        await status_msg.edit_text(f"❌ An error occurred: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is starting…")
    app.run_polling()


if __name__ == "__main__":
    main()