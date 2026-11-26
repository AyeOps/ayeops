# Sentinel cannot deliver its messages

The last message of the sentinel was not accepted by Telegram, or by Slack with
`alerting.contact: slack`. The next time the central host
dies, the sentinel will notice but you will not hear it, except through the dead-man ping.

## What to do

1. `docker compose logs sentinel` on the sentinel's host: `telegram_failed` lines carry the
   HTTP status (the token is never logged).
2. `401`: the bot token in `AYE_TELEGRAM_BOT_TOKEN` is wrong or was revoked; `400 chat not
   found` or `403`: `AYE_TELEGRAM_CHAT_ID` is wrong, or the bot was removed from the chat.
   Compare with the values of the Grafana contact point, which should be the same.
3. A connection error: the sentinel's host cannot reach `api.telegram.org`; check its DNS and
   outbound firewall.
4. With contact `slack`: `slack_failed` lines carry the status (the webhook URL is never
   logged). `403`: a workspace admin restricts the webhook, or it was revoked; `404`: the
   webhook, its app or its channel is gone; `410`: the channel was archived. Make a new webhook
   and put it into `AYE_SLACK_WEBHOOK_URL` in the `.env` of the sentinel's and of the Grafana
   host. A connection error: the host cannot reach `hooks.slack.com`.
5. After fixing the `.env`, `docker compose up -d sentinel`. The message is retried every round
   until it is delivered, and the alert clears with the next delivered one.
