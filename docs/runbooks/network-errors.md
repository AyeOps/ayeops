# Packet errors or drops above 1 %

More than 1 % of the packets of a declared interface have been errors or drops for 15 minutes.

## What to do

1. `ip -s link show <device>` and `ethtool -S <device>`: which counter grows (CRC errors,
   missed, dropped).
2. CRC or frame errors: a bad cable, connector or switch port; replace them one at a time.
3. Receive drops: ring buffers too small for bursts (`ethtool -g`), or a CPU too busy to service
   the interface.
4. Wi-Fi: errors grow with a weak signal, see [wifi-signal-weak](wifi-signal-weak.md).
