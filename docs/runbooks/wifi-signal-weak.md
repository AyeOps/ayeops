# Wi-Fi signal below -75 dBm

The Wi-Fi interface has received its access point at less than -75 dBm for 15 minutes. Below
about -70 dBm throughput drops and latency spikes; below -80 dBm connections break.

## What to do

1. `AyeOps / Internet`, row `Link` (`AyeOps / Інтернет`, row `Лінк` with `locale: uk`):
   signal, bitrate and network over the same minutes; on the host, `iw dev <device> link`
   shows the same numbers.
2. Move the host or the access point, prefer 5 GHz for short distances and 2.4 GHz through
   walls, or add a mesh node.
3. For a server, use a cable: Wi-Fi is a link you watch, not one you trust.
