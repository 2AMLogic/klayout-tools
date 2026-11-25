#!/usr/bin/env bash
# Install one cached Debian package set without using the runner's apt sources.
set -euo pipefail

if [[ $# != 1 || $1 != /* || ! -d $1 ]]; then
    echo "usage: ci-apt-install-cached.sh <absolute archive directory>" >&2
    exit 2
fi
shopt -s nullglob
archives=("$1"/*.deb)
if (( ${#archives[@]} == 0 )); then
    echo "No cached Debian packages in $1" >&2
    exit 1
fi

# apt checks the entire transaction against installed versions before unpacking.
# A refusal leaves the ordinary mirror-backed installer a consistent database.
exec apt-get -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=- \
    --no-remove -y install "${archives[@]}"
