#!/bin/bash
# Run one test id against each patch in /mut/commits; prints "<id> PASS|FAIL". Local validation only.
for p in /mut/commits/*.patch; do
  id=$(basename "$p" .patch)
  rm -rf /work && cp -r /src /work && cd /work
  git apply "$p" || { echo "$id APPLYFAIL"; continue; }
  if PYTHONPATH=/work timeout 600 python -m pytest -q -p no:cacheprovider "$TEST_ID" >/tmp/l.txt 2>&1; then echo "$id PASS"; else echo "$id FAIL $(tail -1 /tmp/l.txt)"; fi
done
