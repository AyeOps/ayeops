# Sentinel cannot reach Telegram

The last message of the sentinel was not accepted by Telegram. The next time the central host
dies, the sentinel will notice but you will not hear it, except through the dead-man ping.

## What to do

1. `docker compose logs sentinel` on the sentinel's host: `telegram_failed` lines carry the
   HTTP status (the token is never logged).
2. `401`: the bot token in `AYE_TELEGRAM_BOT_TOKEN` is wrong or was revoked; `400 chat not
   found` or `403`: `AYE_TELEGRAM_CHAT_ID` is wrong, or the bot was removed from the chat.
   Compare with the values of the Grafana contact point, which should be the same.
3. A connection error: the sentinel's host cannot reach `api.telegram.org`; check its DNS and
   outbound firewall.
4. After fixing the `.env`, `docker compose up -d sentinel`. The message is retried every round
   until it is delivered, and the alert clears with the next delivered one.
