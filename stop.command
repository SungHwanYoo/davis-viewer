#!/bin/zsh
if /opt/homebrew/bin/tmux has-session -t davis-viewer 2>/dev/null; then
  /opt/homebrew/bin/tmux kill-session -t davis-viewer
  echo "DAVIS viewer sharing stopped."
else
  echo "No detached DAVIS viewer session is running."
  echo "If you started it in a Terminal window, press Control-C there."
fi
