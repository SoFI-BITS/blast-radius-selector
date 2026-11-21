#!/bin/bash
# Label every patch in /mut/commits by running the real suite (inside br-cov).
# Writes /mut/labels/<id>.json with failing nodeids. Local validation only.
set -u
mkdir -p /mut/labels
for p in /mut/commits/*.patch; do
  id=$(basename "$p" .patch)
  [ -f "/mut/labels/$id.json" ] && continue
  rm -rf /work && cp -r /src /work && cd /work
  if ! git apply "$p" 2>/tmp/err; then echo "{\"apply_failed\": true}" > "/mut/labels/$id.json"; continue; fi
  LABEL_OUT="/mut/labels/$id.json" PYTHONPATH=/work:/tools timeout 900 python -m pytest -p labelplugin \
     -q -p no:cacheprovider -n 4 --timeout 300 tests >/tmp/log.txt 2>&1
  echo "$id $(tail -1 /tmp/log.txt)"
done
