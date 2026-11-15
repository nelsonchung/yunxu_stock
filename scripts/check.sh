#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m compileall -q stockwatch tests
python3 -m unittest discover -s tests -v
node --check static/app.js
git diff --check
