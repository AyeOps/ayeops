# GPU memory above 95 %

More than 95 % of a GPU's memory has been in use for 15 minutes. The next allocation fails with
an out-of-memory error, often in a process that did nothing wrong.

## What to do

1. `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv`: who holds it.
2. Look for leaks: a model server that loads models and never unloads them, a stuck job.
3. Limit per-process memory (framework settings, smaller batches) or move a workload to another
   GPU.
