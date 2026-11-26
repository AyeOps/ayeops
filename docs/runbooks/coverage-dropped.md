# Line coverage dropped more than 2 points

The latest line coverage reported on the watched branches is more than two percentage points
below its 30-day median.

## What to do

1. «Coverage trend» on `AyeOps / Quality` names the suite and the day it dropped.
2. The commit of that report is on «Latest runs» (`report="coverage"`).
3. Usual causes: new code merged without tests, tests that are now skipped, or a coverage
   configuration that started counting a new directory.
4. A deliberate change, such as counting a new package, settles once it becomes the median.
