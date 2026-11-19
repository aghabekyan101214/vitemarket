#!/usr/bin/env bash
# Deploy one backend release on the production VM.
#
#   CI:   ssh deploy@<host> <tag>      (authorized_keys forces this script; the tag arrives
#                                       in SSH_ORIGINAL_COMMAND)
#   Hand: sudo -u deploy /srv/showcase/infra/scripts/deploy.sh 2026.11.19-abc1234
#
# Steps: check out the commit the image was built from (so compose + this script match the
# image), set API_TAG, pull, start (migrate runs first; api/worker start only if it succeeds),
# wait for the api healthcheck. If the new release is not healthy, roll api/worker back to the
# previous tag without touching the schema (migrations are backward compatible by policy,
# docs/runbooks/deploy.md).
set -euo pipefail

# Everything runs inside main(): bash has parsed the whole function before `git checkout`
# can replace this file on disk.
main() {
  local tag="${1:-${SSH_ORIGINAL_COMMAND:-}}"
  if [[ ! "$tag" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9a-f]{7}$ ]]; then
    echo "usage: deploy.sh <YYYY.MM.DD-sha7>, got: ${tag:-<empty>}" >&2
    exit 2
  fi
  local sha="${tag##*-}"

  cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.."
  exec 9>/tmp/showcase-deploy.lock
  flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

  local -a C=(docker compose -f docker-compose.prod.yml --env-file .env.prod)
  local prev
  prev="$(sed -n 's/^API_TAG=//p' .env.prod)"
  echo "==> deploying $tag (previous: ${prev:-none})"

  git fetch --quiet origin main
  git checkout --quiet --detach "$sha"
  set_tag "$tag"

  "${C[@]}" pull --quiet api
  if "${C[@]}" up -d --remove-orphans && wait_healthy "${C[@]}"; then
    docker image prune -f --filter "until=168h" >/dev/null
    echo "==> deployed $tag"
    "${C[@]}" ps
    return 0
  fi

  echo "==> $tag is not healthy; recent logs:" >&2
  "${C[@]}" logs --tail 80 migrate api worker >&2 || true
  if [[ -n "$prev" && "$prev" != "$tag" && "$prev" =~ ^[0-9]{4}\. ]]; then
    echo "==> rolling back api/worker to $prev" >&2
    set_tag "$prev"
    git checkout --quiet --detach "${prev##*-}"
    "${C[@]}" up -d --no-deps api worker
    wait_healthy "${C[@]}" || echo "==> rollback is not healthy either" >&2
  fi
  exit 1
}

set_tag() { sed -i "s/^API_TAG=.*/API_TAG=$1/" .env.prod; }

# Wait up to 3 minutes for the api container's Docker healthcheck.
wait_healthy() {
  local id state
  for _ in $(seq 1 90); do
    id="$("$@" ps -q api)"
    state="$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo starting)"
    [[ "$state" == healthy ]] && return 0
    [[ "$state" == unhealthy ]] && return 1
    sleep 2
  done
  return 1
}

# Same line as the call: bash never reads past it from a file that changed underneath.
main "$@"; exit $?
