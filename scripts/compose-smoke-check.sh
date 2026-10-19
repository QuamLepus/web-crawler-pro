#!/usr/bin/env bash
# Validate the core Compose stack without launching the crawler CLI, whose
# container command intentionally performs an external sample crawl.
set -euo pipefail

compose_files=(-f docker-compose.yml)
if [[ "${WCP_GHCR_OVERLAY:-0}" == "1" ]]; then
  compose_files+=(-f docker-compose.ghcr.yml)
fi
compose=(docker compose "${compose_files[@]}")

wait_for() {
  local description="$1"
  shift
  printf 'Waiting for %s...\n' "$description"
  for attempt in $(seq 1 90); do
    if "$@"; then
      printf 'Ready: %s\n' "$description"
      return 0
    fi
    sleep 2
  done
  printf 'Timed out waiting for %s\n' "$description" >&2
  "${compose[@]}" ps >&2 || true
  "${compose[@]}" logs --no-color >&2 || true
  return 1
}

check_redis() {
  [[ "$("${compose[@]}" exec -T redis redis-cli ping 2>/dev/null || true)" == "PONG" ]]
}
check_postgres() {
  "${compose[@]}" exec -T postgres pg_isready -U crawler -d crawler >/dev/null 2>&1
}
check_minio() {
  curl --fail --silent http://127.0.0.1:9000/minio/health/live >/dev/null
}
check_elasticsearch() {
  curl --fail --silent 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=5s' \
    | grep -Eq '"status"[[:space:]]*:[[:space:]]*"(yellow|green)"'
}
check_prometheus() {
  curl --fail --silent http://127.0.0.1:9090/-/healthy >/dev/null
}
check_dashboard() {
  curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null
}
check_celery_ready() {
  local service="$1"
  local output
  output=$("${compose[@]}" logs --no-color "$service" 2>&1 || true)
  grep -qE 'ready\.' <<< "$output"
}
check_beat_started() {
  local output
  output=$("${compose[@]}" logs --no-color beat 2>&1 || true)
  grep -qiE 'beat.*starting|scheduler.*started' <<< "$output"
}

wait_for Redis check_redis
wait_for PostgreSQL check_postgres
wait_for MinIO check_minio
wait_for Elasticsearch check_elasticsearch
wait_for Prometheus check_prometheus
wait_for dashboard check_dashboard
wait_for 'Celery worker' check_celery_ready worker
wait_for 'stealth Celery worker' check_celery_ready worker-stealth
wait_for 'Celery Beat' check_beat_started
