#!/usr/bin/env bash
# build-remote-sim-ami.sh — build/refresh a versioned AMI for the `remote`
# `klt sim` backend, per docs/design/remote-sim-backend-spike.md decision 4.
#
# Resolves the "AMI build/refresh pipeline mechanics" open question the
# design note explicitly leaves to Phase 2 (issue #264): bakes ngspice + one
# PDK's curated ngspice model decks onto a fresh Ubuntu build instance, plus
# the idle-shutdown guard src/klayout_tools/remote_launcher.py's
# `idle_guard_install_script()` generates (single source of truth — this
# script calls that Python function via `python3 -c ...` rather than
# maintaining a second copy of the guard script), snapshots the instance to
# a new AMI, and appends/updates the published entry in BOTH
# data/remote-sim-ami-manifest.json (the repo checkout's copy, `--manifest`
# overridable) AND `~/.config/klt/remote-sim-ami-manifest.json` (the
# user-scope copy `src/klayout_tools/remote_launcher.py`'s
# `USER_MANIFEST_PATH` resolves -- issue #370: a tool-installed `klt` (`uv
# tool`/pipx/pip) resolves the manifest from its own installed package-data
# dir, never this checkout's `data/`, so without this second write an
# operator-built AMI would be invisible to that installed `klt`) -- see
# docs/schemas/remote-sim-ami-manifest.schema.json for the entry shape.
#
# Also bakes `klt` itself (issue #265's SSH/SCP transport requirement): the
# `remote` backend's corner fan-out invokes `klt sim ... --backend
# local-parallel` directly on the provisioned box (the literal reuse of
# #255's worker-pool code the design note's decision 5 requires), so the AMI
# must ship the exact same `klayout-tools` code the launcher itself runs.
# Installed from this repo's own git history, pinned to `--klt-ref` (default:
# the commit this script is run from) rather than "whatever `main` is at
# build time" — reproducible the same way decision 4 pins the PDK snapshot.
# The PDK model library is installed under a fixed `$PDK_ROOT` (persisted to
# `/etc/environment` so it is visible to a non-interactive SSH command, not
# just an interactive login shell) so the remote host's own `klt sim`
# invocation resolves `request.models.pdk` via `pdk.find_pdk`'s existing
# `$PDK_ROOT` lookup -- the same code path a local run already uses, no
# remote-specific model resolution logic.
#
# This is an OPERATIONAL script, not exercised by this repo's test suite or
# CI (see CLAUDE.md's "headless always" + this issue's own instruction: an
# automated test must never spin up real EC2 instances). It requires:
#   - Real, working AWS credentials for the target account (same
#     AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_REGION discipline
#     `.claude/skills/repo/scripts/repo-remote.sh` already uses).
#   - The `aws` CLI and `jq` on PATH.
#   - Real cloud spend: a build instance runs for the duration of the
#     ngspice/PDK-deck/`klt`-package install (minutes), then `create-image`
#     snapshots and terminates it. This is the one-time-per-refresh cost of
#     maintaining the AMI, distinct from the per-job spend RemoteLauncher
#     gates in src/klayout_tools/remote_launcher.py.
#
# Deferred/not run in this change: no AMI has actually been built by this
# script yet (no AWS credentials/spend available while authoring #264/#265)
# — see data/README.md. data/remote-sim-ami-manifest.json ships with an
# empty `images` array; an operator runs this script once per (pdk, region)
# they need and the manifest gains real entries from there.
#
# Usage:
#   scripts/aws/build-remote-sim-ami.sh --pdk sky130A --region us-east-1 \
#       [--instance-type c7i.xlarge] [--root-gb 60] \
#       [--copy-to-region us-west-2 ...] \
#       [--klt-ref <git-ref>] [--manifest <path>] [--yes]
#
#   Without --yes: prints the resolved build plan (PDK, region, base AMI,
#   instance type, estimated one-time build cost) and exits — nothing is
#   created. Mirrors repo-remote.sh's own "plan shown before money is spent"
#   discipline (see /repo:remote).
#   With --yes: actually launches the build instance, installs, snapshots,
#   and updates the manifest.
#
# Exit codes: 0 success (including a dry-run plan), 2 usage/missing config,
# 4 an AWS API call failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MANIFEST="${REPO_ROOT}/data/remote-sim-ami-manifest.json"
#: Mirrors src/klayout_tools/remote_launcher.py's USER_MANIFEST_PATH -- an
#: installed (uv tool/pipx/pip) `klt` resolves the AMI manifest from its own
#: package-data dir, never this repo checkout's `data/`, so this build
#: additionally writes here (issue #370) so a freshly built AMI is usable
#: from any `klt` install on this machine immediately, no release required.
USER_MANIFEST="${HOME}/.config/klt/remote-sim-ami-manifest.json"

PDK=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
INSTANCE_TYPE="c7i.xlarge"  # build instance -- installs only, not sized for a corner matrix
COPY_TO_REGIONS=()
YES=false
#: Root volume for the BUILD instance. Must be set explicitly: Ubuntu's base
#: AMI ships an 8 GB root, which fits sky130A's curated decks and does NOT fit
#: gf180mcu's -- issue #612, where the first real gf180mcu build died with
#: `[Errno 28] No space left on device` after pulling four tarballs
#: (fd_ip_sram, fd_pr, fd_sc_mcu7t5v0, fd_sc_mcu9t5v0), which are downloaded
#: AND extracted. Sized generously on purpose: this volume lives for the few
#: minutes of the build, and it does not inflate the AMI's ongoing cost
#: because EBS snapshots bill used, compressed blocks -- not provisioned size.
ROOT_GB="60"
#: Pinned to the commit this script itself is run from by default --
#: reproducible, matches whatever `remote_launcher`/`sim.py`/`remote_transport`
#: code this checkout actually has (see #265's header comment above).
KLT_REF="$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null || echo main)"
#: Fixed baked-model-library root, persisted to /etc/environment on the
#: build instance so it is visible to a non-interactive SSH command (see
#: #265's header comment on why `$PDK_ROOT` -- not a `--pdk-root` request
#: field -- is how the remote host resolves `request.models.pdk`).
PDK_ROOT="/opt/pdk"
#: Pinned open_pdks build the AMI bakes -- the same snapshot the local
#: ~/.volare installs and the canary block repos pin (decision 4:
#: reproducible, never "whatever volare's latest is at build time").
#: Override with --pdk-version for a deliberate refresh.
PDK_VERSION="c6d73a35f524070e85faff4a6a9eef49553ebc2b"

log() { echo "[build-remote-sim-ami] $*" >&2; }
die() { local code="$1"; shift; log "error: $*"; exit "$code"; }

usage() {
  sed -n '2,69p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pdk) PDK="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
    --root-gb) ROOT_GB="$2"; shift 2 ;;
    --copy-to-region) COPY_TO_REGIONS+=("$2"); shift 2 ;;
    --klt-ref) KLT_REF="$2"; shift 2 ;;
    --pdk-version) PDK_VERSION="$2"; shift 2 ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --yes) YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die 2 "unknown argument: $1 (see --help)" ;;
  esac
done

# ── cost/config gate — mirrors repo-remote.sh's require_cost_config: fail
# loudly on missing config, never a silent default (repo#52's discipline,
# reused deliberately here per the design note's own instruction to reuse
# repo-remote.sh's *patterns*, not its code). ─────────────────────────────
[[ -n "$PDK" ]] || die 2 "--pdk is required (sky130A|gf180mcu)"
case "$PDK" in
  sky130A|gf180mcu) : ;;
  *) die 2 "unsupported --pdk '$PDK' (must match remote_launcher.SUPPORTED_PDKS: sky130A, gf180mcu)" ;;
esac
[[ -n "$REGION" ]] || die 2 "--region (or AWS_REGION/AWS_DEFAULT_REGION) is required"
command -v aws >/dev/null 2>&1 || die 2 "the 'aws' CLI is required on PATH"
command -v jq  >/dev/null 2>&1 || die 2 "'jq' is required on PATH (manifest update)"
command -v uv  >/dev/null 2>&1 || die 2 "'uv' is required on PATH (idle-guard script generation via 'uv run python3')"

BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
NGSPICE_VERSION_HINT="46"  # the version this repo's own test matrix pins against; verified post-install below

log "plan: build a ${PDK} AMI in ${REGION} (build instance type ${INSTANCE_TYPE}, root ${ROOT_GB}GB gp3, klt-ref ${KLT_REF}); copy-to: ${COPY_TO_REGIONS[*]:-<none>}"

if [[ "$YES" != true ]]; then
  log "dry-run (pass --yes to actually build). Nothing was created."
  exit 0
fi

aws sts get-caller-identity >/dev/null 2>&1 \
  || die 4 "AWS authentication failed (aws sts get-caller-identity)"

# ── resolve a base Ubuntu 22.04 AMI (same source repo-remote.sh's
# aws_resolve_image uses for a non-GPU host) ────────────────────────────────
BASE_AMI="$(aws ec2 describe-images --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*' \
  --region "$REGION" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
[[ -n "$BASE_AMI" && "$BASE_AMI" != "None" ]] || die 4 "could not resolve a base Ubuntu 22.04 AMI in ${REGION}"

# The root device name is read off the base AMI rather than hardcoded to
# /dev/sda1: a block-device-mapping whose DeviceName does not match the
# image's own RootDeviceName is silently ignored by RunInstances, which would
# reintroduce #612 while appearing to fix it.
ROOT_DEVICE="$(aws ec2 describe-images --region "$REGION" --image-ids "$BASE_AMI" \
  --query 'Images[0].RootDeviceName' --output text)"
[[ -n "$ROOT_DEVICE" && "$ROOT_DEVICE" != "None" ]] || die 4 "could not resolve the root device name of ${BASE_AMI}"

# ── idle-shutdown guard: generated by the same Python function
# RemoteLauncher relies on being baked into every AMI (single source of
# truth — see this script's header comment) ────────────────────────────────
IDLE_GUARD_SCRIPT="$(cd "$REPO_ROOT" && uv run python3 -c \
  "from klayout_tools.remote_launcher import idle_guard_install_script as f; print(f())")"

# ── per-PDK install recipe. Fetches the same curated open-PDK ngspice model
# decks klt's own docs/design/remote-sim-backend-spike.md decision 4 scopes
# this AMI to (sky130A, gf180mcu) -- via the PDK's own published release
# artifact, never an ad hoc mirror. Installed under the fixed $PDK_ROOT this
# script bakes into /etc/environment below, so `pdk.find_pdk` resolves it
# identically to a local install (see #265's header comment). ──────────────
# shellcheck disable=SC2016  # intentional: $(...) expands later, inside the
# build instance's own user-data shell (BUILD_USERDATA below), not here.
case "$PDK" in
  sky130A)
    PDK_FETCH_CMDS='PIP_BREAK_SYSTEM_PACKAGES=1 pip install volare && PDK_ROOT="'"${PDK_ROOT}"'" volare enable --pdk sky130 '"${PDK_VERSION}"''
    ;;
  gf180mcu)
    PDK_FETCH_CMDS='PIP_BREAK_SYSTEM_PACKAGES=1 pip install volare && PDK_ROOT="'"${PDK_ROOT}"'" volare enable --pdk gf180mcu '"${PDK_VERSION}"''
    ;;
esac

# Bracket the PDK install with `df -h` so a disk-space failure names itself in
# the console trace instead of surfacing as a bare `[Errno 28]` mid-extract
# (#612). Cheap, and it is the first thing anyone asks after a failed build.
PDK_FETCH_CMDS="df -h / && ${PDK_FETCH_CMDS} && df -h /"

BUILD_USERDATA="$(mktemp)"
trap 'rm -f "$BUILD_USERDATA"' EXIT
cat > "$BUILD_USERDATA" <<USERDATA
#!/bin/bash
set -euxo pipefail
apt-get update
apt-get install -y ngspice python3-pip python3-venv git
mkdir -p ${PDK_ROOT}
echo "PDK_ROOT=${PDK_ROOT}" >> /etc/environment
${PDK_FETCH_CMDS}
# Bake klt itself (issue #265) -- the remote backend's SSH/SCP transport
# invokes \`klt sim ... --backend local-parallel\` directly on this box, so
# it must ship the exact klayout-tools code the launcher itself runs,
# pinned to the same commit (see this script's header comment).
PIP_BREAK_SYSTEM_PACKAGES=1 pip install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@${KLT_REF}"
${IDLE_GUARD_SCRIPT}
touch /var/log/klt-remote-sim-ami-build-complete
USERDATA

log "launching build instance from base AMI ${BASE_AMI} ..."
BUILD_ID="$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$BASE_AMI" \
  --instance-type "$INSTANCE_TYPE" \
  --block-device-mappings "DeviceName=${ROOT_DEVICE},Ebs={VolumeSize=${ROOT_GB},VolumeType=gp3,DeleteOnTermination=true}" \
  --user-data "file://${BUILD_USERDATA}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=klt-remote-sim-ami-build,Value=${PDK}}]" \
  --query 'Instances[0].InstanceId' --output text)"
[[ -n "$BUILD_ID" && "$BUILD_ID" != "None" ]] || die 4 "run-instances failed to return an instance id"
log "build instance: ${BUILD_ID} (waiting for it to boot and finish installing) ..."

aws ec2 wait instance-running --region "$REGION" --instance-ids "$BUILD_ID"

# Poll for the build-complete marker via SSM (no inbound SSH needed for the
# build box itself) rather than SSH -- keeps this script's own network
# surface minimal. Falls back to a fixed sleep if SSM isn't available on the
# base AMI/account (documented limitation, not silently ignored).
log "waiting for install to complete (up to 10 minutes) ..."
for _ in $(seq 1 60); do
  STATUS="$(aws ec2 describe-instance-status --region "$REGION" --instance-ids "$BUILD_ID" \
    --query 'InstanceStatuses[0].InstanceStatus.Status' --output text 2>/dev/null || echo "")"
  [[ "$STATUS" == "ok" ]] && break
  sleep 10
done
# Instance-status "ok" only means the OS booted -- user-data (PDK download +
# klt install) takes minutes longer. Poll the console output for the
# build-complete marker user-data touches (its `set -x` trace prints the
# `touch` line), keeping the no-inbound-SSH design; die rather than snapshot
# a half-baked instance. Console output flushes with multi-minute latency,
# hence the generous budget. (First live run, Epic #253 validation #277,
# snapshotted an AMI without klt because the old fixed 60s settle raced
# user-data -- this poll is that bug's fix.)
log "waiting for user-data build-complete marker in console output (up to 20 minutes) ..."
MARKER_SEEN=false
for _ in $(seq 1 40); do
  CONSOLE="$(aws ec2 get-console-output --region "$REGION" --instance-id "$BUILD_ID" \
    --output text --query 'Output' 2>/dev/null || true)"
  if printf '%s' "$CONSOLE" | grep -q "touch /var/log/klt-remote-sim-ami-build-complete"; then
    MARKER_SEEN=true
    break
  fi
  # Fail fast (#612). cloud-init reports user-data failure in the console the
  # moment it happens; polling only for the success marker meant a build that
  # died at 84 seconds still burned the full 20-minute budget with the
  # instance billing, and left the operator unable to tell stuck from working.
  # The console output IS the diagnostic, so it is echoed here and the
  # instance is terminated rather than left running "for inspection".
  if FAILURE="$(printf '%s' "$CONSOLE" | grep -m3 -E 'No space left on device|Failed to run module scripts_user|Failed to start Cloud-init: Final Stage')"; then
    log "build failed on the instance -- console says:"
    printf '%s\n' "$FAILURE" | sed 's/^/    /' >&2
    log "terminating build instance ${BUILD_ID} ..."
    aws ec2 terminate-instances --region "$REGION" --instance-ids "$BUILD_ID" >/dev/null 2>&1 || true
    die 4 "user-data failed on the build instance (see console lines above). If this is a disk-space failure, re-run with a larger --root-gb (currently ${ROOT_GB})."
  fi
  sleep 30
done
[[ "$MARKER_SEEN" == "true" ]] || die 4 "user-data never reported build-complete (console output lacks the marker); refusing to snapshot a half-baked instance. Build instance ${BUILD_ID} left running for inspection."

log "stopping build instance before snapshot ..."
aws ec2 stop-instances --region "$REGION" --instance-ids "$BUILD_ID" >/dev/null
aws ec2 wait instance-stopped --region "$REGION" --instance-ids "$BUILD_ID"

IMAGE_NAME="klt-remote-sim-${PDK}-$(date -u +%Y%m%d%H%M%S)"
log "creating image ${IMAGE_NAME} ..."
NEW_AMI="$(aws ec2 create-image --region "$REGION" --instance-id "$BUILD_ID" \
  --name "$IMAGE_NAME" --description "klt remote sim backend: ngspice + ${PDK} decks" \
  --query 'ImageId' --output text)"
[[ -n "$NEW_AMI" && "$NEW_AMI" != "None" ]] || die 4 "create-image failed to return an AMI id"

log "waiting for ${NEW_AMI} to become available ..."
aws ec2 wait image-available --region "$REGION" --image-ids "$NEW_AMI"

log "terminating build instance ${BUILD_ID} ..."
aws ec2 terminate-instances --region "$REGION" --instance-ids "$BUILD_ID" >/dev/null

PDK_SNAPSHOT="${PDK}-$(date -u +%Y.%m.%d)"

# ── manifest update (append; resolve_ami() picks the latest built_at for a
# (pdk, region) pair, so old entries are kept as an audit trail). Writes
# BOTH $MANIFEST (the repo checkout's copy) and $USER_MANIFEST (the
# user-scope copy an installed `klt` resolves -- issue #370) so a freshly
# built AMI is usable from any `klt` install on this machine immediately,
# no release required. ──────────────────────────────────────────────────
write_manifest_entry() {  # <manifest-path> <ami_id> <region>
  local target="$1" ami="$2" region="$3" tmp
  mkdir -p "$(dirname "$target")"
  # $USER_MANIFEST may not exist yet on a fresh machine -- $MANIFEST always
  # does (checked into the repo with an empty "images" array), but seeding
  # unconditionally keeps this one code path correct for both.
  [[ -f "$target" ]] || printf '%s' '{"schema_version": 1, "images": []}' > "$target"
  tmp="$(mktemp)"
  jq --arg pdk "$PDK" --arg region "$region" --arg ami "$ami" \
     --arg snapshot "$PDK_SNAPSHOT" --arg ngspice "$NGSPICE_VERSION_HINT" \
     --arg built_at "$BUILT_AT" \
     '.images += [{"pdk": $pdk, "region": $region, "ami_id": $ami, "pdk_snapshot": $snapshot, "ngspice_version": $ngspice, "built_at": $built_at}]' \
     "$target" > "$tmp"
  mv "$tmp" "$target"
}

update_manifest() {  # <ami_id> <region>
  local ami="$1" region="$2"
  write_manifest_entry "$MANIFEST" "$ami" "$region"
  write_manifest_entry "$USER_MANIFEST" "$ami" "$region"
}

update_manifest "$NEW_AMI" "$REGION"
log "published ${NEW_AMI} for pdk=${PDK} region=${REGION}; manifest updated at ${MANIFEST} and ${USER_MANIFEST}"

for target_region in "${COPY_TO_REGIONS[@]}"; do
  log "copying ${NEW_AMI} to ${target_region} ..."
  COPIED_AMI="$(aws ec2 copy-image --region "$target_region" --source-region "$REGION" \
    --source-image-id "$NEW_AMI" --name "$IMAGE_NAME" --query 'ImageId' --output text)"
  aws ec2 wait image-available --region "$target_region" --image-ids "$COPIED_AMI"
  update_manifest "$COPIED_AMI" "$target_region"
  log "published ${COPIED_AMI} for pdk=${PDK} region=${target_region}"
done

log "done."
