#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'usage: %s {provision|status|verify-precondition|stop} --host <ssh-alias>\n' "$0" >&2
  exit 2
}

command_name=${1:-}
[[ -n "$command_name" ]] || usage
shift
[[ ${1:-} == "--host" && -n ${2:-} ]] || usage
host=$2
shift 2
[[ $# -eq 0 ]] || usage
[[ $host =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  printf 'invalid ssh alias\n' >&2
  exit 2
}

root=/opt/incidentlens-target
remote() { ssh -- "$host" "$@"; }

case "$command_name" in
  verify-precondition)
    remote "test ! -e /opt/incidentlens && test ! -e $root/../incidentlens && test -d /opt"
    ;;
  provision)
    "$0" verify-precondition --host "$host"
    remote "sudo install -d -m 0755 $root"
    tar -C infra -czf - acceptance | remote "sudo tar -xzf - -C $root --strip-components=1"
    remote "cd $root && sudo docker compose -f docker-compose.yml -f compose.cloud.yaml up -d --build"
    remote "docker ps --format '{{.Ports}}' | grep -Ev '(^|,)127\\.0\\.0\\.1:' && exit 1 || true"
    ;;
  status)
    remote "cd $root && sudo docker compose -f docker-compose.yml -f compose.cloud.yaml ps"
    ;;
  stop)
    remote "cd $root && sudo docker compose -f docker-compose.yml -f compose.cloud.yaml down"
    ;;
  *) usage ;;
esac
