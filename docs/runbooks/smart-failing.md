# SMART health check failing

The drive's own health self-assessment (`smartctl -H`) reports a failure. The drive predicts
that it will fail; treat its data as at risk now.

## What to do

1. Check that the latest backup of everything on the drive is complete and restorable.
2. `sudo smartctl -a /dev/<device>`: read the failing attributes and the error log.
3. Copy the data off (`ddrescue` for a degraded drive) and replace the drive. On RAID or ZFS,
   replace the member and let it resilver.
4. Do not run long self-tests on a failing drive before the data is safe: they add wear.
