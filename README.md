# Pocket FM Telegram Bot

A Telegram bot built with `python-telegram-bot` and `httpx` that fetches and sends free episodes of Pocket FM shows as MP3 files.

## Deploy to Render (Free Tier)

1. Fork or push this repository to your GitHub account.
2. Go to [Render Dashboard](https://dashboard.render.com/).
3. Click **New +** -> **Background Worker** (or **Web Service** if preferred, but worker/cron is best for long-polling bots).
4. Connect your GitHub repository.
5. Set the following configurations:
   - **Name**: `pocketfm-telegram-bot`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
6. Add Environment Variables:
   - `TELEGRAM_BOT_TOKEN`: Your Telegram Bot Token obtained from [@BotFather](https://t.me/BotFather).
7. Click **Create Background Worker**.
