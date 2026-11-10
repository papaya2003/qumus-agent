#!/usr/bin/env bash
# Keep an SSH tunnel from this computer to the reactor Lab Console open.
#
#   REACTOR_SSH=qumus@100.64.0.1 scripts/reactor_tunnel.sh
#
# Opens 127.0.0.1:${LOCAL_PORT:-18772} here -> 127.0.0.1:8772 on Olares (the Lab
# Console, base path /lab/). The brain then uses
#   ReactorConsole("http://127.0.0.1:18772/lab/")
# Redials after a drop. Ctrl-C stops it. Needs key login (no password prompt).
set -u
: "${REACTOR_SSH:?set REACTOR_SSH=user@olares-host (e.g. qumus@100.64.0.1)}"
LOCAL_PORT="${LOCAL_PORT:-18772}"
REMOTE_PORT="${REMOTE_PORT:-8772}"
SSH_KEY="${SSH_KEY:-}"
key_args=()
[ -n "$SSH_KEY" ] && key_args=(-i "$SSH_KEY" -o IdentitiesOnly=yes)

trap 'echo "tunnel stopped"; exit 0' INT TERM
delay=2
while true; do
  echo "$(date '+%F %T') opening 127.0.0.1:${LOCAL_PORT} -> ${REACTOR_SSH} 127.0.0.1:${REMOTE_PORT}"
  start=$(date +%s)
  ssh -N "${key_args[@]}" \
    -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$REACTOR_SSH"
  code=$?
  # A tunnel that lived for a while resets the backoff.
  if [ $(( $(date +%s) - start )) -gt 60 ]; then delay=2; fi
  echo "$(date '+%F %T') tunnel ended (ssh exit ${code}); redial in ${delay}s"
  sleep "$delay"
  delay=$(( delay < 30 ? delay * 2 : 30 ))
done
