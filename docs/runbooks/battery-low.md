# Battery below 20 % and not on AC

A laptop host runs on battery below 20 %.

## What to do

1. Plug it in, or expect a host-down alert soon.
2. If the charger is connected but `ayeops_power_on_ac` is 0, check the supply under `/sys/class/power_supply`.
