# Security support of a component ends within 90 days

The release cycle of a component on this host gets security fixes for less than 90 more days,
according to [endoflife.date](https://endoflife.date): the alert's `date` label is the first day
without them (the `eolFrom` date of the cycle). The [eol](../modules.md#eol) module reads the
cycle from the image tag (`postgres:14` is PostgreSQL 14), from the host's `/etc/os-release`
(`component="os"`), or from the `components` the manifest lists. Nothing is broken yet; this is
the window in which an upgrade can still be planned instead of rushed.

The alert fires once per component and cycle, and stays on until the upgrade or until the date
passes, when [eol-passed](eol-passed.md) takes over.

## What to do

1. Which component, and how long: `ayeops_eol_security_days_left{host="<host>"} < 90`, with
   `component`, `product`, `cycle` and `date`. The cycle page is
   `https://endoflife.date/<product>`, with the supported cycles and their dates.
2. Plan the upgrade to a supported cycle, preferably the newest with long support:
   - an image (`postgres:14-alpine`): bump the tag in the compose file or let Renovate's pull
     request from [updates](../modules.md#updates) do it. A database major (PostgreSQL,
     MariaDB, MongoDB) needs a dump and restore or `pg_upgrade`, not only a new tag: test it on
     a copy first, the [offsite](../modules.md#offsite) restore drill is one;
   - the base of an image (`python:3.13-alpine3.20` tracks Alpine 3.20 too): move to the tag on
     a newer base, or rebuild the project's image on one;
   - the host OS (`component="os"`): schedule a release upgrade (`do-release-upgrade` on Ubuntu,
     a new `sources.list` suite on Debian) or a fresh host, with a backup first;
   - a manual component: upgrade it and update its `cycle` in the manifest.
3. Deploy. `aye render` reads the new tag and the agent tracks the new cycle after the next
   deploy; the alert resolves at the next textfile write, within an hour.
4. Some distributions sell support past this date (Ubuntu Pro, Debian ELTS, vendor contracts);
   endoflife.date lists it as extended support, which this module does not count. If such a
   contract covers the component, list it under the module's `exclude` (an image) or remove the
   manual entry, and say in the manifest why.
