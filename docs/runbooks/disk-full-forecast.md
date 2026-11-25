# Disk full within 7 days

At the pace of the last 24 hours, a filesystem that is already more than half full runs out of
space within 7 days. The forecast is a straight line through one day of samples, so a one-off
bulk write can trigger it; a steady trend is the case it is meant for.

## What to do

1. Check the trend on the hosts dashboard: a steady slope or one jump?
2. Find the grower: `du -xh --max-depth=1 <mountpoint> | sort -h | tail`, `docker system df`.
3. Usual suspects: logs without rotation, database growth, backups kept locally, metric or log
   retention, a cache without a limit.
4. Fix the cause or grow the volume before [disk-full](disk-full.md) fires.
