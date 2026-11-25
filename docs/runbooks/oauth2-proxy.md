# Sign-in gate (oauth2-proxy)

Every ingress route without a login of its own asks oauth2-proxy through Traefik's `forwardAuth`
middleware `sso` before it answers. The gate fails closed: when oauth2-proxy is down, those UIs
answer with an error instead of opening without a sign-in. Grafana keeps working, since it signs
people in itself.

## Target down (`job="oauth2-proxy"`)

vmagent could not scrape `oauth2-proxy:9180`, and the gated UIs most likely answer `500`.

1. `docker compose ps oauth2-proxy` on the host: is it running and healthy?
2. `docker compose logs --tail 100 oauth2-proxy`. A start-up error names the setting: a cookie
   secret that is not 16, 24 or 32 bytes after base64 decoding, a missing client id, or with
   `oidc` an issuer it cannot reach for discovery.
3. `AYE_AUTH_CLIENT_ID`, `AYE_AUTH_CLIENT_SECRET` and `AYE_AUTH_COOKIE_SECRET` must be set in
   `.env`; `docker compose up` refuses to start without them.
4. To reach a UI while the gate is down, use the SSH tunnel to `127.0.0.1:<port>` on the host. Do
   not add the route to `open` to get in: that opens it to everyone who can reach the name.

## 403 after the provider's sign-in

The account signed in, but its address is not in `auth.emails`.

1. Check the address in the log line `Permission Denied` or `invalid authentication`.
2. Add it to `auth.emails` in the manifest (lowercase), `aye render`, copy
   `auth/emails.txt` to the host and `docker compose restart oauth2-proxy`.
3. In Grafana the same account is refused with "login failed" by `role_attribute_strict`; the
   same change fixes both. An address in `emails` but not in `admins` gets `Viewer` there.

## CSRF errors on the callback

`unable to obtain CSRF cookie` or `CSRF token mismatch` on `https://auth.<domain>/oauth2/callback`.

1. The sign-in took longer than 15 minutes, or the browser dropped cookies for `.<domain>`: start
   again from the UI, not from a bookmarked callback URL.
2. The callback name must be below the same domain as the UI: the cookie lives on `.<domain>`, so
   a UI on another domain never gets it back.
3. Each sign-in has its own CSRF cookie, so parallel tabs do not clash. If it still happens, clear
   the cookies of `.<domain>` and sign in once.

## Redirect URI mismatch at the provider

The provider refuses with `redirect_uri_mismatch`. Register every callback on the OAuth client:
`https://auth.<domain>/oauth2/callback` for oauth2-proxy and `<grafana root_url>/login/google`
(`/login/generic_oauth` with `provider: oidc`) for Grafana. With the errors module and Google, also
`<errors url>/accounts/google/login/callback/` for GlitchTip.
