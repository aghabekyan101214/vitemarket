#!/usr/bin/env bash
# Start a production image with dummy production settings and check it serves /health/live
# and the OpenAPI document. No database is needed (liveness does not touch it).
#   infra/scripts/smoke-test-image.sh ghcr.io/org/showcase-api:2026.11.11-abc1234
set -euo pipefail
image="${1:?usage: $0 <image:tag>}"
name="showcase-smoke-$$"

docker run -d --name "$name" \
  -e APP_ENV=production \
  -e DATABASE_URL=postgresql+asyncpg://smoke:smoke@127.0.0.1:5432/smoke \
  -e JWT_SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
  -e STORAGE_ENDPOINT_URL=http://127.0.0.1:9 \
  -e STORAGE_ACCESS_KEY_ID=smoke -e STORAGE_SECRET_ACCESS_KEY=smoke \
  -e STORAGE_BUCKET=smoke -e STORAGE_PUBLIC_BASE_URL=https://media.invalid \
  "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null' EXIT

probe='import urllib.request,sys; r=urllib.request.urlopen("http://127.0.0.1:8000"+sys.argv[1],timeout=2); print(r.status)'
for _ in $(seq 1 30); do
  if docker exec "$name" python -c "$probe" /health/live >/dev/null 2>&1; then
    docker exec "$name" python -c "$probe" /openapi.json >/dev/null 2>&1 \
      && echo "openapi: served (docs enabled)" || echo "openapi: not served (docs disabled)"
    user="$(docker exec "$name" id -un)"
    [ "$user" != "root" ] || { echo "image runs as root" >&2; exit 1; }
    echo "smoke test passed: $image (user=$user)"
    exit 0
  fi
  sleep 1
done
echo "API did not become live; logs:" >&2
docker logs "$name" >&2
exit 1
