# No successful endoflife.date fetch for 3 days

`ayeops_eol_last_success_timestamp_seconds` on this host is more than three days old: the
[eol](../modules.md#eol) agent has not fetched every tracked product from endoflife.date for
three days, although it retries every hour after a failure. The days left it writes still come
from the last data in its cache volume and stay right for dates that did not move, but a cycle
whose end date changed since, or a component added since, is not covered. Silence here is not a
clean result.

An agent that has never fetched counts from its first start, so a new host stays quiet for three
days unless every fetch fails.

## What to do

1. `ayeops_eol_fetch_success{host="<host>"} == 0` names the products whose last fetch failed;
   `docker compose logs --tail 20 eol-agent` shows the `failed` map of each attempt with the
   reason (`HTTP 404`, `URLError`, `TimeoutError`, `not JSON`).
2. `HTTP 404` for one product: the product name is wrong, in a manual entry of `components`
   (endoflife.date names them in lower case with dashes: `amazon-linux`, `rocky-linux`), or the
   product was removed. Fix the entry and deploy.
3. All products failing: the host cannot reach `https://endoflife.date`. Check outbound HTTPS and
   DNS from the host (`docker compose exec eol-agent python -c "import urllib.request;
   print(urllib.request.urlopen('https://endoflife.date/api/v1/').status)"`), a proxy or
   firewall, and https://github.com/endoflife-date/endoflife.date for an outage. A self-hosted
   mirror of the API goes into the module's `api` setting.
4. `docker compose ps eol-agent` should be running and healthy; `node-exporter` must read the
   textfile, or `target-down` fires as well.
5. `docker compose exec -e AYE_EOL_ONCE=1 eol-agent python -I /app/eol_agent.py` fetches now
   and prints the result; the alert resolves on the next scrape after a successful fetch.
