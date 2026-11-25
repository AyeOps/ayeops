# On-call bridge cannot open incident topics

The oncall module has `topics: true`, but the bridge cannot create forum topics in the ops group
`ops_chat_id`. Incidents still reach the person on call in direct messages, with repeats, quiet
hours and the acknowledge button; only the topic per incident in the group is missing.

## What to do

1. `docker compose logs --tail 50 oncall-bridge` on the host shows an
   `oncall-topics-unavailable` line with the reason: `not-a-forum`, `no-rights` or `no-chat`.
2. `not-a-forum`: in the group's settings in Telegram, turn on Topics. A group with topics is
   always a supergroup, and its id starts with `-100`; when Telegram converted the group, the id
   changed, so put the new one into `ops_chat_id`.
3. `no-rights`: make the bridge's bot (the one of `AYE_ONCALL_TELEGRAM_TOKEN`) an administrator
   of the group with the right to manage topics.
4. `no-chat`: the bot is not a member of the group, or `ops_chat_id` is wrong. Add the bot, or
   fix the id and run `aye deploy`.
5. The bridge checks again every hour and at every start; the alert clears once
   `ayeops_oncall_topics_available` is 1. To check at once:
   `docker compose restart oncall-bridge`.
