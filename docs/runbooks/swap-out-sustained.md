# Sustained swap-out

The kernel has been writing more than 256 pages per second (about 1 MiB/s with 4 KiB pages) to
swap for 10 minutes. Available memory can still look fine on such a host: the kernel swaps out
because something keeps asking for more than fits. Large hosts are not immune; a database buffer
pool sized close to the physical memory does exactly this.

## What to do

1. `vmstat 5`: the `so` column confirms the swap-out; `free -h` shows what is left.
2. `ps aux --sort=-rss | head` and `docker stats --no-stream`: who grew.
3. Compare the declared reservations (`reserved`, `mem_limit`) with reality; the `memory-budget`
   rule keeps their sum within 85 % of the host memory.
4. Shrink the biggest consumer (buffer pools, worker counts, caches) or add memory. Lowering
   `vm.swappiness` hides the symptom, not the cause.
