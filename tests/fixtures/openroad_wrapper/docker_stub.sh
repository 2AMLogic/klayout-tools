#!/usr/bin/env bash
printf '%s\n' "$@" >>"$DOCKER_ARGV_LOG"
if [[ "$1" == "container" && "$2" == "inspect" ]]; then
    cat "$DOCKER_ARGV_LOG.owner"
    exit
fi
prev=""
for a in "$@"; do
    if [[ "$prev" == "--cidfile" ]]; then
        printf '%s' "STUB_CID_STRING" >"$a"
    fi
    if [[ "$prev" == "--label" ]]; then
        printf '%s' "${a#*=}" >"$DOCKER_ARGV_LOG.owner"
    fi
    prev="$a"
done
# Drain the client's stdin into a sink when asked: the wrapper must forward it.
if [[ "$1" == "run" && -n "${DOCKER_STDIN_SINK:-}" ]]; then
    cat >>"$DOCKER_STDIN_SINK"
fi
# Hold only on `run`: the "container" is what runs long; a kill must
# complete immediately like the real runtime's.
if [[ "$1" == "run" && -n "${DOCKER_HOLD_S:-}" ]]; then
    if [[ -n "${STUB_RUNNING:-}" ]]; then
        echo "$PPID" >"$STUB_RUNNING.tmp"
        mv "$STUB_RUNNING.tmp" "$STUB_RUNNING"
    fi
    sleep "$DOCKER_HOLD_S"
elif [[ "$1" == "kill" && "$2" == "STUB_CID_STRING" && -n "${STUB_RUNNING:-}" ]]; then
    rm -f "$STUB_RUNNING"
fi
exit "${DOCKER_EXIT_CODE:-0}"
