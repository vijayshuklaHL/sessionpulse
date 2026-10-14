#!/bin/sh
# Re-record the showcase video on fictional data (never your real ~/.claude).
# Needs: python3, node 20+, google-chrome, ffmpeg. Output: ./showcase.mp4
set -e
HERE=$(cd "$(dirname "$0")" && pwd); SRC=$(cd "$HERE/../../src" && pwd)
WORK=${1:-$(mktemp -d)}; W=$WORK/world; mkdir -p "$WORK/frames"
python3 "$HERE/make_demo.py" "$W"
env HOME=/home/maya CLAUDE_CONFIG_DIR=$W/home/.claude XDG_CONFIG_HOME=$W/config XDG_DATA_HOME=$W/data \
  PYTHONPATH="$SRC" python3 -m sessionpulse serve >/dev/null 2>&1 & SRV=$!
google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars --user-data-dir="$WORK/chrome" \
  --remote-debugging-port=9334 about:blank >/dev/null 2>&1 & CH=$!
sleep 3
node --experimental-websocket "$HERE/record.mjs" "$W" "$WORK/frames" 9334
kill $CH $SRV
python3 -c "import json,os; [os.kill(l['pid'],15) for l in json.load(open('$W/live.json'))]" || true
ffmpeg -y -loglevel error -f concat -safe 0 -i "$WORK/frames/list.txt" \
  -vf "fps=30,scale=1920:1080:flags=lanczos,format=yuv420p" -c:v libx264 -crf 18 -preset slow -movflags +faststart showcase.mp4
echo "wrote showcase.mp4 (work files in $WORK)"
