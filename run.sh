#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

URL="${1:-}"

if [ -z "$URL" ]; then
  echo "Usage: ./run.sh \"https://example.com/path/to/directory/\" [filename prefix filter] [download.py options]"
  echo "Example: ./run.sh \"https://example.com/files/\" \"The\""
  echo "Example: ./run.sh \"https://example.com/files/\" \"The Vampire Diaries\" --chrome-binary \"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\""
  exit 1
fi

shift

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
pip install -r requirements.txt
python download.py "$URL" "$@"
