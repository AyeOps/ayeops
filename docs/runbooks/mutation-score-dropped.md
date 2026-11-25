# Mutation score dropped more than 3 points

The latest mutation score is more than three percentage points below its 30-day median. The
score is killed plus timed-out mutants over all mutants that ran, without the ignored ones.

## What to do

1. «Survivors by module» on `AyeOps / Quality` names the modules with surviving mutants.
2. Run the mutation tool on that module locally and look at the surviving mutants; each one is
   a change of the code that no test notices.
3. Write the test that kills it. Weakening an assertion or excluding the module only hides the
   hole.
4. A report that lost its killed mutants, such as `mutmut results` without `--all true`, is
   rejected by `aye report` instead of reporting a score of zero.
