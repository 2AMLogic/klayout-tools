- **`klt extract --deck sg13g2` extracts the spiral inductor as a three-terminal
  `inductor` device instead of merging its terminals** (#6726, 2026-11-18): an
  IHP `inductor2` winding is plain TopMetal2 under an `IND` marker, so its two
  terminals came out as one net (`merged_net_labels` `a|b`) with no device and no
  warning. The deck now recognises the winding (marker 27/0, `inductor2*` text,
  `LA`/`LB` pins), cuts it out of the top metals, and extracts `devices[].class`
  `inductor` with terminals `a`/`b`/`sub` (`sub` is the NWell net over a well,
  otherwise `vsubs`); `device_classes` gains `inductor`. `klt lvs` reads the
  `XD_$n a b vsubs inductor` card back as that device given a deck, so a bridge
  between the ports is a mismatch. Additive: a marker with an `inductor*` label
  that is not recognised (`inductor3`, an `LC` pin) keeps the old merge and adds
  a `warnings[]` entry. `27/25` and `63/0` move from `ignored_layers` to
  `device_recognition_only_layers`. Connectivity only -- `w`/`s`/`d`/turns are
  not measured.
