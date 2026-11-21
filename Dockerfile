# Blast Radius selector. The scored image reads patches and a prebuilt map
# (map/blast.map + map/exec_phase.json) built from a per-test coverage run of
# sqlglot at eb11aec4be8cb2122afe713a4816662c3f0fb2fa (see README.md).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY vendor/sqlglot-src.tar.gz /tmp/src.tar.gz
RUN mkdir -p /app/src && tar -xzf /tmp/src.tar.gz -C /app/src && rm /tmp/src.tar.gz \
 && python -m compileall -q /app/src
COPY map/ /app/map/
COPY gate/tpcds_gate.py /app/gate/tpcds_gate.py
# fingerprint of the unpatched pre-execution pipeline of test_execute_tpcds
RUN cd /app/src && PYTHONHASHSEED=0 PYTHONPATH=/app/src python -S /app/gate/tpcds_gate.py \
      /app/src/tests/fixtures/optimizer/tpc-ds --baseline > /app/gate/baseline.json \
 && python -c "import json;b=json.load(open('/app/gate/baseline.json'));assert len(b)==99 and not any(x.get('res','').startswith('ERR') for x in b)"
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "-E", "/app/selector.py"]
