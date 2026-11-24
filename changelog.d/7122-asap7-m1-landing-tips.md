- **ASAP7 M1 via landing spacing** (#7122, 2026-11-21).
  On ASAP7, `place-and-route` can resolve M1 tip spacing between opposing V1 landings by extending their placed pad edges while preserving cut coverage and conductor isolation. The merge reports the number of pairs it repaired in `via_pad_trim.m1_landing_pairs_extended`.
