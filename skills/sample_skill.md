# System status report

Report the machine's current status in one Telegram message.

## Steps
1. Run `uptime` and `df -h /` via execute_shell.
2. Run `ps -A -o rss= | awk '{s+=$1} END {print s/1024 " MB"}'` for total RSS.
3. Summarize the results in 3 short Korean lines (load, disk, memory).
