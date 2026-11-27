- **`klt sim` batch deck jobs report `stamp_scope: "image"` when a deck reads a declared input** (#7981).
  `environment.remote.worker_environment.pdk.stamp_scope` said `executed` for
  a `decks[]` job whose `.include`/`.lib` closure staged one of a unit's
  `inputs[]`, although the engine ran the client's bytes and never the image's
  PDK. One such unit anywhere in the job now reports `image`; the stamp fields
  and `models_staged` keep their worker-observed values. An unrelated declared
  input beside a host-resolved PDK reference leaves the scope `executed`.
