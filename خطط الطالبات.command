#!/bin/bash
# Double-clicked from Finder: start the local page and open the browser.
# Resolves its own directory so it works from the Desktop or the Dock too.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "البيئة غير موجودة في $(pwd)/.venv"
  echo "افتحي المشروع وشغّلي:  python3 -m venv .venv && .venv/bin/python -m pip install -e ."
  read -r -p "اضغطي Enter للإغلاق..."
  exit 1
fi

.venv/bin/python -m khutat.web
