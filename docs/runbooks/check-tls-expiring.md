# TLS certificate of a check expires in less than 14 days

A [check](../checks.md) that speaks TLS, an `https://` `http` check or an `smtp` check with
`tls: starttls` or `tls: tls`, saw a certificate that expires within 14 days; automatic renewal
normally happens at 30. The `check` label names it, `<service>-<check>`.

## What to do

1. Look at the certificate from the host that runs the check (`from`, or the service host):
   - `http`: `openssl s_client -connect www.example.com:443 -servername www.example.com </dev/null | openssl x509 -noout -enddate -subject`
   - `smtp` with STARTTLS (25, 587): `openssl s_client -starttls smtp -connect mail.example.com:587 -servername mail.example.com </dev/null | openssl x509 -noout -enddate -subject`
   - `smtp` with implicit TLS (465): `openssl s_client -connect mail.example.com:465 -servername mail.example.com </dev/null | openssl x509 -noout -enddate -subject`
2. Check the renewal of whatever issues it: the ACME client, the CDN or proxy, or the mail
   server's own certificate job. A mail server often reads its certificate only at start, so
   after a renewal reload it (`postfix reload`, `doveadm reload`, or restart the container)
   and run the command above again.
3. The alert clears once the probe sees the new certificate, within one interval of the check.
