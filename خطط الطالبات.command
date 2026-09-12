#!/bin/bash
# Double-clicked from Finder: start the local page and open the browser.
# Resolves its own directory so it works from the Desktop or the Dock too.
cd "$(dirname "$0")" || exit 1

# Local settings that are not committed — the association's working-folder
# link, and anything else that differs per machine.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ ! -x .venv/bin/python ]; then
  echo "البيئة غير موجودة في $(pwd)/.venv"
  echo "لتهيئتها:  python3 -m venv .venv && .venv/bin/python -m pip install -e ."
  read -r -p "Enter للإغلاق..."
  exit 1
fi

.venv/bin/python -m khutat.web
