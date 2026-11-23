#!/bin/sh
# Cycle Control as a background service: cc start | stop | restart | window | log
label=com.cyclecontrol.app; plist="$HOME/Library/LaunchAgents/$label.plist"; dom="gui/$(id -u)"
case "${1:-window}" in
  start)   launchctl bootstrap "$dom" "$plist" 2>/dev/null || launchctl kickstart "$dom/$label" ;;
  stop)    launchctl bootout "$dom/$label" ;;
  restart) launchctl kickstart -k "$dom/$label" ;;
  window)  cd "$(dirname "$0")" && exec uv run main.py ;;
  log)     tail -f "$HOME/.cycle-control/cycle-control.log" ;;
  *) echo "usage: cc start|stop|restart|window|log" >&2; exit 1 ;;
esac
