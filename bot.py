Import asyncio
import logging
import os
import re
import urllib.parse
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
BASE = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://pocketfm.com",
    "Referer": "https://pocketfm.com/",
    "Content-Type": "application/json",
    "app-version": "180",
    "app-client": "consumer-web",
    "platform": "web",
    "app-name": "pocket_fm"
}
NUM_KEYS = ("seq_number", "episode_number", "sequence_number", "index", "ep_no", "episode_no")


async def fetch_show_episodes(show_id: str):
    """Fetches all episodes and metadata for a given Pocket FM show ID."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        await c.get("https://pocketfm.com/search", headers={"User-Agent": UA})
        tok = urllib.parse.unquote(c.cookies.get("auth-token") or "").strip('"')
        headers = {**BASE, "Authorization": f"Bearer {tok}"}
        
        eps, ptr, lp, title = [], 0, None, ""
        for _ in range(50):
            r = await c.get(
                "https://api.pocketfm.com/v2/content_api/show.get_details",
                params={"show_id": show_id, "curr_ptr": ptr, "info_level": "max"},
                headers=headers
            )
            if r.status_code != 200:
                logger.error(f"HTTP Error: {r.status_code}")
                break
            j = r.json()
            if not j.get("result"):
                break
            res = j["result"][0]
            if ptr == 0:
                title = res.get("show_title", "Unknown Show")
                lp = res.get("episode_locking_point", 0)
            
            page = res.get("stories", [])
            if not page:
                break
            eps += page
            nxt = j.get("next_ptr", res.get("next_ptr"))
            ptr = nxt if isinstance(nxt, int) and nxt > ptr else ptr + len(page)
            if len(page) < 5:
                break
        return title, lp, eps


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Pocket FM Free Episode Downloader Bot!\n\n"
        "Send me a Pocket FM **Show Link** or **Show ID**, and I will fetch and send all free episodes as MP3 files."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    m = re.search(r"[a-f0-9]{40}", text)
    show_id = m.group(0) if m else text

    if len(show_id) < 10:
        await update.message.reply_text("❌ Invalid Show ID or link provided. Please try again.")
        return

    status_msg = await update.message.reply_text("🔍 Fetching show details and checking free episodes...")
    
    try:
        title, lp, eps = await fetch_show_episodes(show_id)
        if not eps:
            await status_msg.edit_text("❌ Could not find any episodes for this show ID.")
            return

        # Filter free episodes based on locking point or flags
        free_eps = []
        for i, e in enumerate(eps, 1):
            n = next((int(e[k]) for k in NUM_KEYS if str(e.get(k, "")).isdigit()), i)
            is_free = True
            if lp is not None and n > lp:
                is_free = False
            # Check explicit lock flags if present
            flags = {k: e[k] for k in ("is_locked", "locked", "is_premium", "is_paid", "is_coin_locked") if k in e}
            if any(flags.values()):
                is_free = False

            if is_free:
                free_eps.append((n, e))

        await status_msg.edit_text(
            f"📚 *{title}*\n"
            f"🔓 Locking Point: Episode {lp}\n"
            f"📦 Total Episodes: {len(eps)} | Free Episodes Found: {len(free_eps)}\n\n"
            f"📤 Starting upload of free episodes...",
            parse_mode="Markdown"
        )

        if not free_eps:
            await update.message.reply_text("⚠️ No free episodes found for this show.")
            return

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for n, e in free_eps:
                ep_title = e.get("story_title") or e.get("title") or f"Episode {n}"
                # Extract media/audio stream URL safely
                audio_url = e.get("audio") or e.get("stream") or e.get("hls") or e.get("media")
                
                # If nested inside media dictionaries
                if not audio_url and isinstance(e.get("media_meta"), dict):
                    audio_url = e["media_meta"].get("url")

                if not audio_url:
                    # Look for any key containing stream/url/audio in dict values
                    for val in e.values():
                        if isinstance(val, str) and val.startswith("http") and any(ext in val for ext in [".mp3", ".m4a", ".aac", "m3u8"]):
                            audio_url = val
                            break

                if not audio_url:
                    continue

                try:
                    # Download audio stream chunk or file
                    resp = await client.get(audio_url)
                    if resp.status_code == 200:
                        file_name = f"Ep_{n}_{ep_title[:30].strip()}.mp3".replace("/", "_")
                        with open(file_name, "wb") as f:
                            f.write(resp.content)
                        
                        with open(file_name, "rb") as audio_file:
                            await update.message.reply_audio(
                                audio=audio_file,
                                title=ep_title,
                                performer=title,
                                caption=f"🎧 Ep {n}: {ep_title}"
                            )
                        os.remove(file_name)
                    await asyncio.sleep(1) # Prevent flooding Telegram API
                except Exception as ex:
                    logger.error(f"Failed to send episode {n}: {ex}")

        await update.message.reply_text("✅ All free episodes have been sent!")

    except Exception as e:
        logger.exception(e)
        await status_msg.edit_text(f"❌ An error occurred: {str(e)}")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
