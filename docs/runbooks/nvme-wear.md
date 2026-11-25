# NVMe wear above 80 %

`Percentage Used` of an NVMe drive passed 80 %: the drive has written most of its rated
endurance (TBW). It usually keeps working past 100 %, but the vendor no longer promises it.

## What to do

1. `sudo smartctl -a /dev/<device>`: read `Percentage Used`, `Data Units Written` and
   `Available Spare`.
2. Find the writer: `iotop -oa`, database WAL and temp files, swap, container logs, metric
   storage.
3. Plan the replacement while the drive is healthy, and move write-heavy data off it.
