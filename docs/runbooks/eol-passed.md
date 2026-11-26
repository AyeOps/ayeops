# A component no longer gets security fixes

The release cycle of a component on this host is past its end of security support, according to
[endoflife.date](https://endoflife.date): its `eolFrom` date has passed, or the product lists the
cycle as end of life. New vulnerabilities in it will not be fixed upstream, and scanners such as
[vulns](../modules.md#vulns) stop seeing fixes for it, because there are none. See the
[eol](../modules.md#eol) module.

The alert is a `warning`, never a page: nothing changed tonight, and an upgrade is daytime work.
It keeps firing until the component runs a supported cycle.

## What to do

1. Which component: `ayeops_eol_security_ended{host="<host>"} == 1`; the date is on
   `ayeops_eol_security_days_left` (negative days since). `docker compose logs --tail 5
   eol-agent` lists the `ended` components of the last fetch.
2. Upgrade it as [eol-soon](eol-soon.md) describes: the image tag, the base image, the host OS or
   the manual entry. Treat it before other upgrades if it is reachable from the internet.
3. When an upgrade is not possible yet, write down why and until when, and keep the alert; a
   paid extended support contract (Ubuntu Pro, Debian ELTS) is the one reason to `exclude` the
   image or drop the manual entry.
