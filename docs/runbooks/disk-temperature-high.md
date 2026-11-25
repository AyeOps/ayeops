# Drive above 60 °C

A drive has reported more than 60 °C for 15 minutes. Hard disks age fast above about 50 °C;
NVMe drives throttle around 70 °C and slow down without telling anyone.

## What to do

1. `sudo smartctl -a /dev/<device>`: current, maximum and warning temperatures.
2. Check the airflow: fans, dust filters, a drive cage without air, an NVMe slot under the GPU.
3. Give an NVMe drive a heatsink; raise the fan curve of a NAS.
4. If the load is a burst (backup, scrub), consider moving it to a cooler time of day.
