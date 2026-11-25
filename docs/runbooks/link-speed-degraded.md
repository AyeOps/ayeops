# Link speed below its weekly maximum

A declared wired interface negotiated a lower speed than it had during the last 7 days, for
example 100 Mbit/s instead of 1 Gbit/s. The usual cause is a damaged cable or a pair that lost
contact: gigabit needs all four pairs, 100 Mbit/s works on two.

## What to do

1. `ethtool <device>`: current speed, duplex and advertised modes.
2. Replace the cable, reseat both ends, try another switch port.
3. If the lower speed is intended (a new switch), the alert clears itself after 7 days.
