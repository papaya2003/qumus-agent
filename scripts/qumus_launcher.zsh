#!/bin/zsh
# Shared body of every "Start Qumus.command" / "Stop Qumus.command".
# They work from ~/Projects/Qumus, from inside v2, by double-click or in any
# terminal; all of them run scripts/start_qumus.py of this repository.
QUMUS_REPO=${0:A:h:h}
QUMUS_PYTHON="$QUMUS_REPO/.venv/bin/python"
if [[ ! -x "$QUMUS_PYTHON" ]]; then
  print 'Qumus environment is missing. Run python3 scripts/setup_qumus.py from the repository.'
  if [[ -t 0 ]]; then read -r '?Press Return to close.'; fi
  exit 1
fi
"$QUMUS_PYTHON" "$QUMUS_REPO/scripts/start_qumus.py" "$@"
QUMUS_EXIT=$?
if [[ $QUMUS_EXIT -ne 0 && $QUMUS_EXIT -ne 130 && -t 0 ]]; then
  read -r '?Read the message above, then press Return to close.'
fi
exit "$QUMUS_EXIT"
