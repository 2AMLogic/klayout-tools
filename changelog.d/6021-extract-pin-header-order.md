- **`klt extract --pins` orders a drawn pin label containing a literal pipe**
  (#6021, 2026-11-17). A single label drawn as `Y|ybar` can be declared as
  `Y|ybar`; its escaped SPICE token `Y\x7cybar` occupies the declared slot.
  When `Y` and `ybar` are two labels on one net, declare either component
  to promote it. Declaring only their joined display spelling leaves it
  internal. The promoted pin keeps the writer's `Y|ybar` token. Ordering
  also accepts whole tokens already present in the header. For disconnected
  same-label nets (#2000), all matching ports stay promoted in their existing
  relative order, so `pin_count` can exceed the number of declared pins.
