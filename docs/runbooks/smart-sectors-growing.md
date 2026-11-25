# Bad sectors or media errors growing

Within the last day the drive reallocated sectors, found new pending or offline-uncorrectable
sectors (SATA), or logged media errors (NVMe). A single event can be benign; a growing count is
the classic early sign of a dying drive.

## What to do

1. `sudo smartctl -a /dev/<device>`: note `Reallocated_Sector_Ct`, `Current_Pending_Sector`,
   `Offline_Uncorrectable` or `Media and Data Integrity Errors`, and compare with the day before.
2. Verify the backups of the drive.
3. Run a long self-test while the drive is idle: `sudo smartctl -t long /dev/<device>`.
4. If the count keeps growing over the next days, plan a replacement.
