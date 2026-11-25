# Sentinel dead-man ping failing

For 15 minutes the sentinel could not ping `AYE_SENTINEL_PING_URL`. The dead-man service then
reports the sentinel as down although it runs, and, worse, would not notice if it really died.

## What to do

1. `docker compose logs sentinel`: `ping_failed` lines name the error.
2. `ValueError` or `unknown url type`: the variable holds a placeholder such as `replace-me`;
   put the ping URL of the check there (`https://hc-ping.com/<uuid>`) or leave it empty to turn
   the ping off.
3. HTTP 404: the check was deleted or the UUID is mistyped; copy the URL again.
4. A connection error: the host cannot reach the ping service; check its DNS and outbound
   firewall.
5. `docker compose up -d sentinel` after changing the `.env`.

The ping is sent only after a round where every check passed, so while the central host is down
this alert cannot fire anyway: that silence is the dead-man service's job.
