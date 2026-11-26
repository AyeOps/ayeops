# On-call bridge has not reached Telegram

The on-call bridge has not finished a Telegram poll for 10 minutes. Messages may still go out,
but acknowledge buttons do nothing, and if Telegram itself is unreachable no message arrives.
Critical alerts still reach the ops channel through Grafana, so nothing is lost; what is lost
is the repeat and the button.

## What to do

1. `docker compose logs --tail 50 oncall-bridge` on the host. An `oncall-poll-conflict` line
   means another process polls the same bot token, and the bridge stopped polling on purpose.
   `AYE_ONCALL_TELEGRAM_TOKEN` must be a bot of its own, never a bot that something else polls,
   such as the product's bot. Put the right token into `.env` and
   `docker compose up -d --force-recreate oncall-bridge`.
2. `ayeops_oncall_failures_total{op="poll"}` growing means the host cannot reach
   `api.telegram.org`: check the host's internet (`AyeOps / Internet`) and DNS.
3. A revoked token answers `401`: create a new one with BotFather (`/token`) and update `.env`.
4. `docker compose exec oncall-bridge wget -qO- http://127.0.0.1:8080/metrics` shows the last
   poll time; the alert clears a few minutes after it moves again.
