# Network interface down

A declared network interface has been down (no carrier, no association, or not present) for 5
minutes. Only the interfaces listed in `network.interfaces` are watched.

## What to do

1. `ip -br link` and `ip -br addr` on the host.
2. Wired: cable, switch port, and `ethtool <device>` for link detection and negotiated speed.
3. Wi-Fi: `iw dev <device> link`, `nmcli device status`; is the access point up?
4. If the interface is gone on purpose (an undocked laptop, a removed USB adapter), remove it
   from `network.interfaces`.
