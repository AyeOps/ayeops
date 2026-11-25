# On-call bridge fails to deliver

More than two failures of one kind in 15 minutes, by `op`:

| `op` | What failed | Look at |
|---|---|---|
| `relay` | Grafana's alert could not be handed to GoAlert | `docker compose ps goalert` and `docker compose logs --tail 50 goalert`; a `503` means the setup has not got through yet, see `setup` |
| `setup` | the bridge could not write the manifest into GoAlert | `docker compose logs oncall-bridge \| grep oncall-setup`: the `error` names what GoAlert refused; a login failure means `AYE_ONCALL_ADMIN_PASSWORD` changed after the admin was created, change it back or reset it in GoAlert |
| `telegram` | a message or the button removal failed | the bridge's log; Telegram answers `429` when rate limited, and that passes on its own |
| `goalert` | an acknowledge press was refused | the acknowledge API key may be gone: `docker compose restart oncall-bridge` mints a new one |
| `poll` | Telegram polling failed | [oncall-telegram-silent](oncall-telegram-silent.md) |
| `acks` | GoAlert refused or did not answer the read of its alert log for the incident journal | `docker compose ps goalert`; if GoAlert answers but refuses the query, the alert log key is gone or no longer matches its query: `docker compose restart oncall-bridge` mints a new one. Paging is not affected, only `acknowledged` in the [incident journal](../modules.md#incidents) |

Critical alerts still reach the ops channel through Grafana's second route while this lasts.
After a fix, send a test from GoAlert (your profile, the contact method `Telegram oncall bridge`,
Send test): the bridge answers in Telegram.
