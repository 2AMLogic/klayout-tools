- **A Liberty list-merge no longer lets a complex attribute named like a
  template group swallow the header groups after it** (#8060, 2026-11-28).
  `lu_table_template (x);` or `power_lut_template (x);` in a member header
  is now compared like any other header field, so a member that declares it
  and one that lacks it refuse, and the template groups declared after it are
  unioned by name as intended. Previously the attribute armed the template
  scanner, which dropped it and the next header group from the comparison and
  could write a duplicate `lu_table_template` or `input_voltage` group into
  the merged file.
