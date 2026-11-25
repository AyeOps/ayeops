# endoflife.date does not know the cycle of a component

The [eol](../modules.md#eol) module fetched the product of a component from
[endoflife.date](https://endoflife.date), but the product lists no release cycle with the
component's `cycle`, so when its support ends is unknown and [eol-soon](eol-soon.md) and
[eol-passed](eol-passed.md) cannot fire for it. Silence for this component would not mean it is
supported.

Usual causes: a manual entry with a typo or the wrong form (`cycle: "16.4"` where PostgreSQL
cycles are majors such as `16`, `cycle: "22"` where Ubuntu cycles are `22.04`); a pre-release
image (`postgres:19beta1`) whose cycle is not published yet; or a product that renamed its cycles.

## What to do

1. Which one: `ayeops_eol_cycle_known{host="<host>"} == 0`, with `component`, `product` and
   `cycle`.
2. Compare with the cycles the product lists: `https://endoflife.date/api/v1/products/<product>`,
   field `result.releases[].name`, or the page `https://endoflife.date/<product>`.
3. A manual entry: correct `cycle` (or `product`) in the module's `components` and deploy.
4. An image: pin the image to a released version, or `exclude` it while it is a pre-release. If
   the tag clearly names a cycle that the mapping reads wrongly, that is a bug of the mapping
   table in `src/ayeops/eol.py`; report it.
5. The alert resolves at the next textfile write after the deploy, within an hour.
