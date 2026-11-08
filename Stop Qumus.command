#!/bin/zsh
# The same as ~/Projects/Qumus/Stop Qumus.command, for terminals opened in v2.
exec /bin/zsh "${0:A:h}/scripts/qumus_launcher.zsh" --stop "$@"
