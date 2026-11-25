# Sentinel stopped checking

The sentinel answers scrapes but has not finished a round of checks for 5 minutes, so it would
not notice the central host dying. Its `/health` answers 503 in this state.

## What to do

1. `docker compose logs --tail 50 sentinel`: a `round_failed` line shows an error the loop
   survived; no lines at all point to a hung check.
2. `docker compose restart sentinel`.
3. If it stalls again, compare `interval` with the number of checks times `timeout`: a round
   must fit in a few intervals; lower `timeout` or drop a check.
4. Check the host itself: memory (the container is limited to 48 MB), CPU steal on a small VM.
