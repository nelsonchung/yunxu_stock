#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m compileall -q stockwatch tests
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/intraday.js
node --check static/indicators.js
node --check static/technical.js
node --test tests/indicators.test.js
git diff --check
