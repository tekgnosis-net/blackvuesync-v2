#!/usr/bin/env bash
# entrypoint: remaps the dashcam user to PUID/PGID if set, then execs the
# python module with whatever subcommand was passed as CMD. defaults to
# `serve` when CMD is empty (the normal long-running production case).
set -eu

/setuid.sh

if [ $# -eq 0 ]; then
    exec su-exec dashcam python -m blackvuesync_v2 serve
else
    exec su-exec dashcam python -m blackvuesync_v2 "$@"
fi
