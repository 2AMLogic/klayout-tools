- **`klt gen-compose` pad self-notch check rejects only a notch the pad
  introduces, and no longer merges the whole layer per candidate** (#6314,
  #7718, 2026-11-24): the `ports_from_labels` landing search's `self_notch`
  test (#5608) and `compose()`'s own-block pad check (#1520) now share one
  decision. It runs `notch_check` on just the layer polygons the pad touches,
  before and after dropping the pad in, and counts only edge pairs present
  after and absent before. Two consequences. A notch the macro already carries
  elsewhere on the layer (a legitimate clean macro can present one: the deck
  does not model sky130's `m1.3ab` wide-metal exception) no longer fails every
  candidate with a misattributed `pad ... notches the net's own metal`, and
  search and `compose()` agree on such a layer. And the per-candidate cost
  follows the touched polygons instead of the layer's polygon count (2.76 ms
  -> 0.047 ms per candidate on a 2000-polygon layer, about 59x, measured on
  one host; 0.020 ms at 10 polygons, so nearly flat). A pad that narrows or splits
  a pre-existing notch still rejects. Whole polygons are selected rather than
  clipping the layer to a window around the pad because clipping cuts a
  polygon that crosses the window edge and drops notches between pieces of
  what is one polygon on the whole layer. Additive: no JSON field changes,
  `schema_version` unchanged.
