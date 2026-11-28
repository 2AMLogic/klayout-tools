- **`klt lvs` (`klayout-runset`) makes the `DEVICE_LIST` passthrough
  observable** (#5796, 2026-11-17): `reference_conversion` gains
  `passthrough_callees` / `passthrough_cards` for `form: "subckt-call"` (`[]` /
  `0` when nothing passed through), and a `reference.unresolved_subckt_call`
  error is emitted when cards passed through but the runset recognized no
  reference device — previously the cards vanished and only `device.unmatched`
  showed it. Additive; `schema_version` stays `3`, and the #5402 passthrough
  itself is unchanged.
