# nvidia-smi failing

The GPU exporter has not been able to run `nvidia-smi` for 5 minutes. Either the container lost
its access to the driver, or the driver lost the GPU (XID 79, "fallen off the bus", looks like
this).

## What to do

1. On the host: `nvidia-smi`. If it fails there too, read the kernel log:
   `sudo dmesg -T | grep -i -E 'nvrm|xid'`.
2. If the host is fine, recreate the exporter with
   `docker compose up -d --force-recreate nvidia-gpu-exporter`, and check that the NVIDIA
   Container Toolkit is installed and configured (`nvidia-ctk runtime configure --runtime=docker`).
3. After a driver upgrade without a reboot the kernel module and the user-space tools differ;
   reboot.
4. XID 48, 63, 64, 74, 79, 94 or 95 in the kernel log point at hardware or driver faults that
   usually need a reboot and, when they repeat, a hardware check.
