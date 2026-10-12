# deal-tracker
Polls t.me/s/deals and posts Amazon deals (>=40% instant, rest batched after 20 min) to a Telegram channel.
Runs daily 21:00-01:00 IST via GitHub Actions. Secrets: DEALWALA_BOT_TOKEN, AMAZON_TAG, CHANNEL_ID, STATUS_CHAT_ID (optional).
Manual run: Actions > deal-tracker > Run workflow (test_minutes>0, autopost off = dry run, no posts).
