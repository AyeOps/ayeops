# Disk above 80 %

A filesystem has been more than 80 % full for 15 minutes.

## What to do

1. `df -h` and `du -xh --max-depth=1 <mountpoint> | sort -h | tail`.
2. Usual suspects: docker images and build cache (`docker system df`), logs, metric retention.
3. Lower `retention` of the metrics module if storage is the culprit.
