# GPU above 85 °C

An NVIDIA GPU has reported more than 85 °C for 10 minutes. Most consumer cards lower their
clocks here; sustained heat shortens fan and memory life.

## What to do

1. `nvidia-smi -q -d TEMPERATURE,PERFORMANCE,POWER`: temperature, throttle reasons, power draw.
2. Check fans and airflow; a card in a closed case or a server chassis without directed air
   overheats quickly.
3. Cap the power if the workload allows it: `sudo nvidia-smi -pl <watts>`.
