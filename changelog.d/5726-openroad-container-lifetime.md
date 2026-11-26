- **OpenROAD stages clean up their Docker container when interrupted** (#5726,
  2026-11-19). A SIGTERM or SIGHUP to the `klt` driver now reaches the isolated
  engine through the driver's cleanup handler. The generated wrapper also kills
  its container when signalled directly. Regenerate installed wrappers with
  `scripts/install-openroad-docker.sh` to pick up this behavior.
  `openroad-logs/<id>/invocation.json` records a running stage at launch and
  names the container once its ID is known, including for diagnosis after SIGKILL.
