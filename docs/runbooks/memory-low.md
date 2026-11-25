# Memory available below 10 %

MemAvailable stayed below 10 % of MemTotal for 10 minutes.

## What to do

1. `docker stats --no-stream` to find the container using memory.
2. Compare with the `mem_limit` values in the rendered compose file; `aye check` keeps their sum within the host `memory`.
3. Processes outside docker: `ps aux --sort=-rss | head`.
