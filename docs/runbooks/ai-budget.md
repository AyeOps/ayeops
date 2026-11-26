# AI provider budget used

The month-to-date cost of a provider passed 80 % (`ai-budget-80`, warning) or 100 %
(`ai-budget-exceeded`, critical) of its `monthly_budget_usd` in the `ai-usage` settings. The
month is the UTC month, the same one the providers bill.

The budget is yours, not the provider's: crossing it stops nothing. A spend limit set in the
provider's console stops the API when it is reached; this alert comes before that.

## What to do

1. `AyeOps / AI` → «Spent this month by model» names the model, «Spend of the day so far by
   model» shows when it started.
2. Usual causes: a loop that retries a failing request, a prompt that grew (a whole document
   or history resent each call), a larger model than intended, prompt caching that stopped
   hitting («Cache reads among input tokens» fell).
3. Stop or slow the caller, then decide: raise the budget in the manifest, or keep it and fix
   the caller. The provider console's spend limit is the hard stop if you need one tonight.
4. The count starts again on the first day of the next UTC month.
