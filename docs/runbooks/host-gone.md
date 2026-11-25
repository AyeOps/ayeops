# Volatile host gone

A host the manifest marks `volatile` has not had a successful node scrape for longer than its
`volatile` duration (a day for `volatile: true`). `host-down` leaves volatile hosts out, so a
workstation switched off at night or a box on a flaky link does not page; this warning is what
is left when the host stays away for longer than it is allowed to. It also fires when the host
is on but node-exporter answers `up == 0` for that long.

## What to do

1. Is the host meant to be off this long (a holiday, a lab box parked)? Then nothing is broken.
   The warning repeats every 4 hours and resolves by itself a week after the duration, when the
   host's last series leaves the query window; set a longer `volatile` duration (up to `7d`) if
   this is the normal pattern, or retire the host from the manifest if it is not coming back.
2. Is it meant to be on? Treat it as [host-down](host-down.md): power, network, then
   `docker compose ps` on the host for vmagent and node-exporter.
3. Once the host reports again the warning resolves on the next evaluation, and the alerts held
   back while it was down evaluate again: look at what fires on the host in the next minutes.
