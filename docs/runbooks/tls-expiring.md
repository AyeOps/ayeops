# TLS certificate expires in less than 14 days

The certificate served at the probed URL expires within 14 days; automatic renewal normally happens at 30.

## What to do

1. Check the renewal logs of whatever issues the certificate (ACME client, CDN, proxy).
2. Renew by hand if needed, then confirm the new expiry on the probes dashboard.
