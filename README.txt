BTCUSDT 9/21 EMA Telegram alert bot

Files:
- bot.py: one market check, then exits
- requirements.txt: Python dependency
- .github/workflows/bot.yml: runs every 5 minutes and saves state

IMPORTANT:
1. Add GitHub Actions repository secrets named TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
2. Do not upload your Telegram token in any file.
3. The repository is Public, so bot.py strategy source is visible to everyone.
4. GitHub scheduled runs can be delayed; this is not a guaranteed always-on 24/7 server.
5. This bot sends alerts only. It does not place trades.
