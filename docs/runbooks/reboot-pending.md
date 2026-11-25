# Reboot pending for a week

The host has waited 7 days for a reboot: `ayeops_reboot_required{reason="flag"}` means the
distribution asked for one after an update (`/run/reboot-required`), `reason="kernel"` means a
newer kernel is installed than the one running. Until the reboot the host runs the old kernel or
libraries, security fixes included.

## What to do

1. Plan a reboot window; on the central host it also pauses Grafana, alerting and the metrics store.
2. Reboot. The flag clears at the next check, within `reboot_interval` seconds.
3. If `reason="kernel"` stays at 1 after the reboot, the host boots an older kernel than the
   newest installed: check the boot loader default, or remove the kernel that is never booted.
