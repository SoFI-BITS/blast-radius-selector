# Blast Radius selector. The scored image only reads patches and a prebuilt
# map; it never runs sqlglot's tests. The map (map/blast.map) was built from a
# per-test coverage run of sqlglot at eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# with tools/covplugin.py + tools/build_map.py (see README.md).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY vendor/sqlglot-src.tar.gz /tmp/src.tar.gz
RUN mkdir -p /app/src && tar -xzf /tmp/src.tar.gz -C /app/src && rm /tmp/src.tar.gz
COPY map/blast.map /app/map/blast.map
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "-E", "/app/selector.py"]
