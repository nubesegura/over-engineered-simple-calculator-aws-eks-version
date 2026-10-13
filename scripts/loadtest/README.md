# Load test (run from the bastion)

`run.sh` sends POST requests to `add`, `sub`, `mul`, `div` and GET requests to `history` of the EKS backend
(`/api/eks/v1/...`) for a few minutes, so the HorizontalPodAutoscalers add replicas and the Cluster Autoscaler
adds nodes. It uses only `curl`, which the bastion already has: nothing is downloaded or installed.

## Before you start

1. The NAT gateway must exist (the autoscaler cannot add nodes without it; see `templates/README.md`).
2. Get a Cognito **ID token** of a test user on your own computer (the webpage repository documents how) and put
   it in a file on the bastion with a Session Manager session, for example `~/id-token.txt` (`chmod 600`). Delete
   the file afterwards. The token lasts one hour; the script stops with a message when the API answers 401.
3. Open three more sessions to watch: `./run.sh --print-watch` prints the commands.

## Run

```
./scripts/loadtest/run.sh --url https://<api host> --token-file ~/id-token.txt --duration 300 --concurrency 20
./scripts/loadtest/run.sh --url https://<api host> < ~/id-token.txt     # token from the standard input
```

The token is never an argument (so it is not in the shell history nor in `ps`), is never printed, and is held only
in a private temporary header file removed when the script ends. At the end it prints the number of requests by
HTTP status. Mostly `200`; a few `429`, `502`, `503` or `000` while pods start are normal; many `401` or `403` mean
a bad token.

## What to record (acceptance, owner)

`kubectl get hpa -n calculator -w`, `kubectl get pods -n calculator -o wide -w` and
`kubectl get nodes -w` give: replicas reached per workload (above 1, up to 4), the time from the first Pending pod
until the new node is `Ready` (up to 4 nodes), and after the load stops the return to 1 replica (5 minute
stabilization window) and to 2 nodes (about 10 minutes of low use). Custom metrics (requests per second, queue length)
are the next feature, not part of this test.

Notes: the requests of the pods are small (100m CPU), so on two `t4g.medium` nodes even four replicas of every
workload may fit without Pending pods; if no node is added, raise `--concurrency` only after reading the pod and node
counts, and record the result as it is.
