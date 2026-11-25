# A flow is below, above or empty against its usual level

A [baseline alert](../modules.md#flow-baselines) compares one series of a flow (a source, a
category) with the median of the same hour of the week over the last 4 weeks. The rule name says
which side tripped: `<name>` the window is under its share of the usual level, `<name>-zero`
nothing arrived at all, `<name>-spike` far more than usual, `<name>-empty` too many successful
but empty answers. The labels of the alert name the series.

## What to do

1. Open the linked panel of `AyeOps / Flow baselines`: it shows the share of the usual level per
   series. One series down while the others hold points at its source; all of them down points
   at the shared part (the app, its scheduler, the network, the database).
2. For a drop or a zero, check the source by hand: does the upstream still answer, and with
   items? An API that answers 200 with an empty list, or a page whose markup changed so the
   parser matches nothing, keeps every error alert quiet. Count such answers as
   `outcome="ok_empty"` so `<name>-empty` catches the next one.
3. Check the collector itself: its logs, its last run, whether the series is still exported. A
   series that vanished counts as zero. If `host-down` fires for the collector's host, start
   there: a flow split by `host` is held back while its host is down, a flow summed over hosts
   is not.
4. For a spike, look for duplicates and loops: a retry storm, a cursor that no longer advances,
   a source that started returning everything instead of what is new.
5. If the drop is planned (maintenance, a source switched off for a while), add a `quiet` window;
   if a series is gone for good, add a `skip` entry with the reason. A series the panel shows as
   learning never alerts until it has 2 weeks of history.
