#!/usr/bin/env bash
# Load test of the calculator API, run from the bastion (task 6.3). It sends POST requests to the four
# operations and GET requests to the history for a few minutes with plain curl (installed on the bastion), so no
# load tool has to be downloaded. The Cognito ID token is read from a file or from the standard input, kept in a
# header file (mode 0600, in a private directory removed on exit) and given to curl with -H @file: it is never an
# argument of any command, never printed and never written to the shell history.
#
# Usage:
#   run.sh --url https://<api host> [--token-file FILE] [--duration SECONDS] [--concurrency N] [--print-watch]
#   (without --token-file the token is read from the standard input, or typed without echo on a terminal)
#
# Examples:
#   run.sh --url https://api.example.invalid --token-file ~/id-token.txt
#   run.sh --url https://api.example.invalid < ~/id-token.txt
#   run.sh --print-watch            # only prints the commands to watch the scaling
#
# An ID token lasts one hour by default; the run stops early with a clear message when the API answers 401.
set -euo pipefail

BASE_URL=""
TOKEN_FILE=""
DURATION=240
CONCURRENCY=20
PRINT_WATCH_ONLY=false
API_PREFIX="/api/eks/v1"

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

fail() {
  echo "loadtest: $*" >&2
  exit 1
}

print_watch() {
  cat <<'WATCH'

Watch the scaling in other Session Manager sessions of the bastion (read-only commands):
  kubectl get hpa -n calculator -w                        # replicas reached per workload (TARGETS, REPLICAS)
  kubectl get pods -n calculator -o wide -w               # new pods, Pending when no node has room
  kubectl get nodes -L topology.kubernetes.io/zone -w     # node join time (STATUS Ready) and later removal
  kubectl top nodes; kubectl top pods -n calculator       # CPU use against the requests
  kubectl get events -n calculator --sort-by=.lastTimestamp | tail -20   # SuccessfulRescale, FailedScheduling
  kubectl -n kube-system logs deploy/cluster-autoscaler --tail=20        # scale-up and scale-down decisions

Record for the acceptance report (UTC time of each):
  1. load start, the first HPA rescale and the maximum REPLICAS reached per workload (kubectl get hpa)
  2. the first Pending pod, the time the new node appears and becomes Ready (kubectl get nodes), nodes reached (up to 4)
  3. load end, the time the replicas return to 1 (after the 5 minute stabilization window) and the time the extra
     nodes are removed (about 10 minutes after they stay underused), ending with 2 nodes
WATCH
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) BASE_URL="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --duration) DURATION="${2:-}"; shift 2 ;;
    --concurrency) CONCURRENCY="${2:-}"; shift 2 ;;
    --print-watch) PRINT_WATCH_ONLY=true; shift ;;
    -h | --help) usage; exit 0 ;;
    *) fail "unknown argument $1 (the token is never an argument; use --token-file or the standard input)" ;;
  esac
done

if [ "$PRINT_WATCH_ONLY" = "true" ]; then
  print_watch
  exit 0
fi

[ -n "$BASE_URL" ] || fail "--url is required (https://<api host>)"
case "$BASE_URL" in
  https://*) ;;
  *) fail "--url must start with https://" ;;
esac
BASE_URL="${BASE_URL%/}"
[[ "$DURATION" =~ ^[0-9]+$ ]] && [ "$DURATION" -ge 10 ] && [ "$DURATION" -le 1800 ] || fail "--duration must be 10 to 1800 seconds"
[[ "$CONCURRENCY" =~ ^[0-9]+$ ]] && [ "$CONCURRENCY" -ge 1 ] && [ "$CONCURRENCY" -le 100 ] || fail "--concurrency must be 1 to 100"
command -v curl > /dev/null || fail "curl is required"

work_dir="$(umask 077 && mktemp -d)"
cleanup() {
  jobs -p | xargs -r kill 2> /dev/null || true
  rm -rf "$work_dir"
}
trap cleanup EXIT
trap 'echo "loadtest: interrupted" >&2; exit 130' INT TERM

# Read the token without echoing it
token=""
if [ -n "$TOKEN_FILE" ]; then
  [ -r "$TOKEN_FILE" ] || fail "cannot read the token file"
  token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
elif [ ! -t 0 ]; then
  token="$(tr -d '[:space:]')"
else
  read -r -s -p "Cognito ID token: " token
  echo >&2
  token="$(printf '%s' "$token" | tr -d '[:space:]')"
fi
[[ "$token" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]] || fail "the token is empty or is not a JWT (three dot separated parts)"
(umask 077 && printf 'Authorization: Bearer %s\n' "$token" > "$work_dir/headers")
token=""

echo "Load test: ${CONCURRENCY} workers for ${DURATION} s against ${BASE_URL}${API_PREFIX} (started $(date -u +%FT%TZ))"

# One worker: loops until the deadline; appends the HTTP status of every request to its own file
worker() {
  local id="$1" deadline="$2" i=0 op code
  local ops=(add sub mul div add sub mul div history)
  while [ "$SECONDS" -lt "$deadline" ]; do
    op="${ops[$((i % ${#ops[@]}))]}"
    i=$((i + 1))
    if [ "$op" = "history" ]; then
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "@${work_dir}/headers" \
        "${BASE_URL}${API_PREFIX}/history?limit=20" 2> /dev/null)" || code="000"
    else
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -X POST -H "@${work_dir}/headers" \
        -H 'Content-Type: application/json' -d "{\"a\": $((i % 90 + 10)), \"b\": $((i % 7 + 3))}" \
        "${BASE_URL}${API_PREFIX}/${op}" 2> /dev/null)" || code="000"
    fi
    echo "$code" >> "${work_dir}/codes.${id}"
    if [ "$code" = "401" ]; then
      echo "loadtest: the API answered 401: the token expired or is not valid" >&2
      return 0
    fi
  done
}

deadline=$((SECONDS + DURATION))
for ((n = 1; n <= CONCURRENCY; n++)); do
  worker "$n" "$deadline" &
done
wait

echo "Finished $(date -u +%FT%TZ). Requests by HTTP status (000 = no answer):"
if compgen -G "${work_dir}/codes.*" > /dev/null; then
  cat "${work_dir}"/codes.* | sort | uniq -c
else
  echo "  none"
fi
print_watch
