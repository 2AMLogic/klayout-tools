- **`klt place-and-route --backend batch` validates the request before upload**
  (#7583, 2026-11-24): the shared batch preflight now runs the same
  corner-independent request validation as `--backend local` (#7538) — engine,
  `target_stage`, `floorplan`, `constraints`, `seed`, `output_dbu_um`,
  `post_route_sdf` and the rest — for both a `pdk.rebuild_corners` sweep and a
  single unit. A defective request is one `PlaceAndRouteError` with the local
  backend's message and no job submitted, instead of being packaged, launched
  and rediscovered inside every remote job. A request defect is reported before
  a packaging or fleet-config problem. No new flags or fields; the error
  envelope is unchanged. See `docs/cli/place-and-route.md` "Physical rebuild
  per corner".
