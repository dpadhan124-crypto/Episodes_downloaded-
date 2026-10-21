"""
Pocket FM Free Episode Downloader — Telegram Bot
-----------------------------------------------
Fetches a show from Pocket FM, filters free episodes, downloads the HLS
streams via FFmpeg (merging every segment), and uploads complete MP3s.
"""

import asyncio
import logging
import os
import re
import shlex
import subprocess
import tempfile
import urllib.parse                       # <-- FIX: required for urllib.parse.unquote
from typing import List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API_BASE = "https://api.pocketfm.com/v2/content_api/show.get_details"
SEARCH_URL = "https://pocketfm.com/search"

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

FFMPEG_HEADERS = (
    "Referer: https://pocketfm.com/\r\n"
    "Origin: https://pocketfm.com\r\n"
    f"User-Agent: {UA}\r\n"
)

NUM_KEYS = (
    "seq_number",
    "episode_number",
    "sequence_number",
    "index",
    "ep_no",
    "episode_no",
    "number",
)
TITLE_KEYS = ("story_title", "title", "episode_title", "name")
SHOW_TITLE_KEYS = ("show_title", "title", "name")
LOCK_FLAGS = ("is_locked", "locked", "is_premium", "is_paid", "is_coin_locked")

MAX_PAGES = 100
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
FFMPEG_TIMEOUT = 600
TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def check_ffmpeg() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, timeout=10
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def safe_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip(" ._")
    return name[:max_len] or "episode"


def get_episode_number(episode: dict, fallback: int) -> int:
    for key in NUM_KEYS:
        value = episode.get(key)
        if str(value).isdigit():
            return int(value)
    return fallback


def get_episode_title(episode: dict, number: int) -> str:
    for key in TITLE_KEYS:
        value = episode.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Episode {number}"


def is_free(episode: dict, episode_number: int, locking_point: Optional[int]) -> bool:
    if locking_point is not None and episode_number > locking_point:
        return False
    if any(episode.get(flag) for flag in LOCK_FLAGS):
        return False
    return True


def extract_audio_candidates(episode: dict) -> List[str]:
    urls: List[str] = []

    direct_keys = (
        "audio_url", "mp3_url", "file_url", "audio", "stream", "media",
        "hls", "hls_url", "dash_url", "audio_url_hls", "audio_url_mp3",
    )
    for key in direct_keys:
        v = episode.get(key)
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)

    for container_key in ("media_meta", "audio_meta", "streaming", "media", "audio_file"):
        container = episode.get(container_key)
        if isinstance(container, dict):
            for key in ("url", "mp3_url", "audio_url", "hls_url", "hls", "dash_url", "mp3"):
                v = container.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    urls.append(v)

    if not urls:
        for v in episode.values():
            if isinstance(v, str) and v.startswith("http") and (
                ".mp3" in v or ".m3u8" in v or ".m4a" in v
            ):
                urls.append(v)

    def rank(url: str) -> int:
        if ".mp3" in url:
            return 0
        if ".m3u8" in url:
            return 1
        if ".m4a" in url:
            return 2
        return 3

    seen = set()
    unique: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return sorted(unique, key=rank)


# ---------------------------------------------------------------------------
# Pocket FM scraper
# ---------------------------------------------------------------------------

async def _fetch_auth_token(client: httpx.AsyncClient) -> str:
    try:
        await client.get(SEARCH_URL, headers={"User-Agent": UA}, timeout=20)
    except httpx.RequestError as exc:
        logger.warning("Token bootstrap request failed: %s", exc)
    token = urllib.parse.unquote(client.cookies.get("auth-token") or "").strip('"')
    return token


async def fetch_show_episodes(show_id: str) -> Tuple[str, Optional[int], List[dict]]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        token = await _fetch_auth_token(client)
        headers = dict(BASE_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        episodes: List[dict] = []
        ptr = 0
        locking_point: Optional[int] = None
        show_title = "Unknown Show"
        seen_ids = set()

        for page_idx in range(MAX_PAGES):
            params = {"show_id": show_id, "curr_ptr": ptr, "info_level": "max"}

            resp: Optional[httpx.Response] = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await client.get(API_BASE, params=params, headers=headers)
                    if resp.status_code == 200:
                        break
                    logger.warning(
                        "Page %s: HTTP %s (attempt %s/%s)",
                        page_idx, resp.status_code, attempt, MAX_RETRIES,
                    )
                except httpx.RequestError as exc:
                    logger.warning(
                        "Page %s: request error %s (attempt %s/%s)",
                        page_idx, exc, attempt, MAX_RETRIES,
                    )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)

            if resp is None or resp.status_code != 200:
                logger.error("Giving up on page %s after retries.", page_idx)
                break

            try:
                data = resp.json()
            except ValueError:
                logger.error("Page %s: invalid JSON.", page_idx)
                break

            result_list = data.get("result") or []
            if not result_list:
                break

            result = result_list[0]

            if page_idx == 0:
                for key in SHOW_TITLE_KEYS:
                    if isinstance(result.get(key), str):
                        show_title = result[key]
                        break
                locking_point = result.get("episode_locking_point")
                if locking_point is not None:
                    try:
                        locking_point = int(locking_point)
                    except (TypeError, ValueError):
                        locking_point = None

            page_episodes = (
                result.get("stories")
                or result.get("episodes")
                or result.get("data")
                or []
            )
            if not page_episodes:
                break

            for ep in page_episodes:
                ep_id = ep.get("story_id") or ep.get("id") or ep.get("episode_id")
                if ep_id and ep_id in seen_ids:
                    continue
                if ep_id:
                    seen_ids.add(ep_id)
                episodes.append(ep)

            next_ptr = data.get("next_ptr", result.get("next_ptr"))
            if isinstance(next_ptr, int) and next_ptr > ptr:
                ptr = next_ptr
            else:
                ptr += len(page_episodes)

            if len(page_episodes) < 5:
                break

        return show_title, locking_point, episodes


# ---------------------------------------------------------------------------
# FFmpeg download
# ---------------------------------------------------------------------------

def _run_ffmpeg_sync(input_url: str, output_path: str) -> Tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel", "error",
        "-headers", FFMPEG_HEADERS,
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", input_url,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "192k",
        "-f", "mp3",
        output_path,
    ]
    logger.info("ffmpeg %s", " ".join(shlex.quote(c) for c in cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out after {FFMPEG_TIMEOUT}s."
    except FileNotFoundError:
        return False, "FFmpeg not found on PATH."

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        return False, f"FFmpeg exit {result.returncode}: {tail}"

    if not os.path.exists(output_path):
        return False, "FFmpeg reported success but output missing."
    return True, ""


async def download_to_file(url: str, output_path: str) -> Tuple[bool, str]:
    if ".m3u8" in url:
        return await asyncio.to_thread(_run_ffmpeg_sync, url, output_path)

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": UA}
            ) as resp:
                resp.raise_for_status()
                with open(output_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        return True, ""
    except Exception as exc:
        return False, str(exc)


async def download_episode(urls: List[str], output_path: str) -> Tuple[bool, str]:
    last_error = "No candidate URLs."
    for url in urls:
        ok, err = await download_to_file(url, output_path)
        if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 50_000:
            return True, ""
        last_error = err or "Output too small."
        if os.path.exists(output_path):
            os.remove(output_path)
    return False, last_error


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "👋 *Pocket FM Free Episode Downloader*\n\n"
    "Send a Pocket FM show *link* or *show ID* and I'll:\n"
    "  1. Fetch the show metadata\n"
    "  2. Identify free episodes (below the lock point)\n"
    "  3. Download & merge HLS streams with FFmpeg\n"
    "  4. Upload each as a complete MP3\n\n"
    "⚠️ Telegram limits each bot upload to 50 MB per file."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = re.search(r"[a-f0-9]{40}", text, re.IGNORECASE)
    show_id = match.group(0) if match else text

    if len(show_id) < 10:
        await update.message.reply_text("❌ Invalid show ID or link. Please resend.")
        return

    status = await update.message.reply_text("🔍 Fetching show details…")

    try:
        show_title, locking_point, all_eps = await fetch_show_episodes(show_id)
        if not all_eps:
            await status.edit_text("❌ No episodes found for this show.")
            return

        free_eps: List[Tuple[int, dict]] = []
        for idx, ep in enumerate(all_eps, start=1):
            n = get_episode_number(ep, idx)
            if is_free(ep, n, locking_point):
                free_eps.append((n, ep))

        lock_display = locking_point if locking_point is not None else "unknown"
        await status.edit_text(
            f"📚 *{show_title}*\n"
            f"🔓 Locking point: Episode {lock_display}\n"
            f"📦 Total: {len(all_eps)} | 🆓 Free: {len(free_eps)}\n\n"
            f"📤 Starting upload…",
            parse_mode=ParseMode.MARKDOWN,
        )

        if not free_eps:
            await update.message.reply_text("⚠️ No free episodes found for this show.")
            return

        sent, skipped = 0, 0
        with tempfile.TemporaryDirectory(prefix="pocketfm_") as tmpdir:
            for n, ep in free_eps:
                ep_title = get_episode_title(ep, n)
                urls = extract_audio_candidates(ep)
                if not urls:
                    logger.warning("Ep %s: no audio URL found.", n)
                    skipped += 1
                    continue

                out_path = os.path.join(
                    tmpdir, f"Ep_{n}_{safe_filename(ep_title)}.mp3"
                )

                try:
                    ok, err = await download_episode(urls, out_path)
                    if not ok:
                        logger.error("Ep %s: download failed — %s", n, err)
                        skipped += 1
                        continue

                    size = os.path.getsize(out_path)
                    if size > TELEGRAM_MAX_UPLOAD:
                        logger.warning(
                            "Ep %s: %s MB exceeds Telegram limit, skipping upload.",
                            n, size // (1024 * 1024),
                        )
                        skipped += 1
                        continue

                    duration = None
                    if isinstance(ep.get("duration"), (int, float)):
                        duration = int(ep["duration"])

                    with open(out_path, "rb") as fh:
                        await update.message.reply_audio(
                            audio=fh,
                            title=ep_title,
                            performer=show_title,
                            caption=f"🎧 Ep {n}: {ep_title}",
                            duration=duration,
                        )
                    sent += 1

                except Exception as exc:
                    logger.exception("Ep %s failed: %s", n, exc)
                    skipped += 1
                finally:
                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except OSError:
                            pass

                await asyncio.sleep(1)

        await update.message.reply_text(
            f"✅ Finished. Sent: {sent} | Skipped: {skipped}"
        )

    except Exception as exc:
        logger.exception("Show %s failed", show_id)
        try:
            await status.edit_text(f"❌ Error: {exc}")
        except Exception:
            await update.message.reply_text(f"❌ Error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set.")

    if not check_ffmpeg():
        logger.error(
            "FFmpeg is NOT available on PATH — HLS episodes will fail to download. "
            "Make sure the Docker image includes ffmpeg."
        )
    else:
        logger.info("FFmpeg detected ✓")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot is starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()