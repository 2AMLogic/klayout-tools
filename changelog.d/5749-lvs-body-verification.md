- **`klt lvs` `body_verification` reports what it inspected, and never reads
  `"verified"` when it inspected nothing** (#5749, 2026-11-16): the block
  gains an `inspected_count` integer (present in every branch, `0` for
  `"unchecked"`) and two dishonest `"verified"` renders are closed. (1) The
  check selected devices by exact equality with the deck's generic
  `nfet_class`/`pfet_class` only, so under `options.compare_mos_flavour:
  true` — where `split_layout_mos_flavours` has already renamed every
  flavoured device to `{base}__{flavour}` (e.g. `nfet__nvt`) before the body
  check runs — it inspected nothing yet reported
  `status: "verified"`, `device_count: 0`, indistinguishable from a block
  whose every body tie was drawn. Flavoured classes are now inspected like
  the generic ones: the same layout that used to read vacuously `"verified"`
  under `compare_mos_flavour: true` reads `"unverified"` with a non-zero
  `device_count`, agreeing with the `compare_mos_flavour: false` run of the
  identical GDS and deck, and `device_classes` names the extracted class
  (`nfet__nvt`). The `device.body_unverified` warnings and the block still
  come from one determination and now agree per flavoured class too. (2) A
  passives-only layout (no MOS device at all) also read the vacuous
  `"verified"`; zero eligible devices now reads `"unchecked"` with a `reason`
  naming the deck's MOS class names. Genuinely verified layouts are
  unchanged apart from the new field (e.g. `inspected_count: 2` on the
  gf180mcu drawn-tie inverter). The block remains a disclosure field: no
  `status` value is added or removed, and the block does not feed grading —
  `status`, `error_count` and `category_error_counts` are untouched. But
  the previously-skipped flavoured warning now fires, so on a
  `compare_mos_flavour: true` run over a layout with untied flavoured
  bodies `mismatch_count` increases by one per newly-seen class and
  `category_counts` gains a `device.body_unverified` key. A consumer that
  read `body_verification.status: "verified"` as "this run inspected
  bodies" must now also read `inspected_count`, because a pre-#5749
  `"verified"` may have inspected nothing. No `schema_version` change (`docs/schemas/lvs-response.schema.json`
  describes the new field). The determination moved from `lvs_mismatch.py`/
  `lvs_power.py` into the new `klayout_tools/lvs_body.py` with no behavior
  change beyond the above.
