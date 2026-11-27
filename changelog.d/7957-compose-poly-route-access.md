- **`klt gen-compose` rejects a route whose backbone lies outside the metals
  stack when its pin lies on it** (#7957, 2026-11-26): a `"poly"`, `"tap"` or
  `"active"` backbone drawn to a pin on `ExtractionDeck.metals`
  (a poly backbone over an li1 pad, say) was reported `routed: true` with
  `unrouted_nets: []` although no contact joined the two layers, so `klt
  extract` recovered the terminals as separate clusters while still
  promoting the route's own label as a named pin. The net is now reported
  unroutable. The reason identifies the effective backbone layer and explains
  how to change or clear a leg or net override, change an inherited default,
  or select a port on the backbone's own layer. A `ports_from_labels`
  `access.up_to` outside the stack is rejected the same way, and its error
  names `access.up_to` (shared or per-name) rather than a routing field.
  Same-layer landings (a
  poly route to a bare-poly gate) and pins on layers outside both the stack
  and the poly/diffusion roles are unchanged. No JSON field is added or
  renamed, and `schema_version` is unaffected.
