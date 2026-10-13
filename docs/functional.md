# Functional documentation

The EKS version of the calculator is a backend that implements the same API contract as the serverless (`sls`) and container (`ecs`) siblings. The web page (`over-engineered-simple-calculator-webpage`) calls the API host name of its environment with the Cognito ID token; which backend answers is decided only by the weighted Route 53 records owned by `over-engineered-simple-calculator-shared-resources`. This backend is registered there as `eks`. Its history starts empty (no data is migrated from other backends). It is built to be deployed for a few days, studied and destroyed; `dev` is deployed, `prod` is designed and not deployed.

Related: [technical.md](technical.md) (configuration, deployment, runbooks), [architecture.md](architecture.md) (components and decisions).

## API

Base paths: `/api/v1` (neutral, shared by every backend, used by the web page) and `/api/eks/v1` (own prefix). Both behave identically. All requests and responses are JSON.

| Method and path | Purpose |
|---|---|
| `POST /{add, sub, mul, div}` | Body `{"a": <number>, "b": <number>}`. Returns `200` with `calculation_id`, `operation`, `a`, `b`, `result` (strings) and `backend`, the string `eks` (the name of the backend that answered; a constant of this repository, not part of history items) |
| `GET /history?limit=<n>&cursor=<opaque>` | Latest calculations first: `items` (`calculation_id`, `operation`, `a`, `b`, `result`, `occurred_at`) and `next_cursor` (null at the end). `limit` defaults to 20 and is capped at 100 |
| `OPTIONS <any API path>` | CORS preflight; needs no token; answers `204` with the allowed origin of the environment, methods `GET, POST, OPTIONS` and headers `Content-Type, Authorization` |
| `GET /health` | Answered by every pod for the load balancer's target health check and the Kubernetes probes; needs no token and does not touch the database. It has no route on the load balancer, so it cannot be called from the internet |

Rules:

- Operands are numbers between -1e15 and 1e15 with at most 15 decimal places, passed as JSON numbers or strings, computed with exact decimal arithmetic and returned as plain decimal strings (no exponent, no floating-point artifacts). The request body is limited to 4096 bytes.
- A calculation is saved before the `200` is returned; saving the same `calculation_id` again keeps one row. A saved row is visible to the next history read.
- `cursor` values are signed by the history service; a value it did not issue is rejected. The signing key is a secret that only the history pod reads; replacing it only invalidates cursors in flight (`400`).
- Every authenticated user may use every operation and read the whole history (same as `sls` and `ecs`).

### Errors

Shape: `{"error": {"code", "message", "request_id"}}`. `request_id` is the load balancer trace id when present, so it can be searched in the pod logs.

| Status | When | Code | Notifies the owner |
|---|---|---|---|
| 400 | Invalid JSON, body not an object, body too large, missing operand, operand not a number, out of range or with too many decimals, invalid `limit` or `cursor` | `VALIDATION_ERROR` | No |
| 400 | `div` with `b` equal to zero | `DIVISION_BY_ZERO` | No |
| 401 | No valid Cognito ID token. Produced by the load balancer, so its body is not the JSON shape above and the request never reaches a pod | (load balancer) | No |
| 404, 405 | Unknown route or method on an API path that reaches a pod (`NOT_FOUND`, `METHOD_NOT_ALLOWED`). A path with no route at all gets the fixed `404` response of the load balancer listener | `NOT_FOUND`, `METHOD_NOT_ALLOWED` | No |
| 500 | Unexpected or persistence error (including a database that cannot be reached within 3 seconds); generic message | `INTERNAL_ERROR` | Yes, throttled (below) |

## Access

Every method except the CORS preflight and `/health` needs `Authorization: Bearer <Cognito ID token>`. The load balancer verifies the signature against the pool keys, the issuer, the expiry, `aud` equal to the app client and `token_use` equal to `id`; rejected requests never reach a pod. The pool and the app client belong to the web page repository. Plain HTTP is redirected to HTTPS; the HTTPS listener accepts TLS 1.3 and TLS 1.2 and uses the shared API certificate. The pods accept traffic only from the load balancer's subnets, so calling a pod directly is not possible.

## Notifications (alerts)

An unexpected error sends an email through the alerts topic with the service, environment, error type, a safe message and the request id. **One alert per error type per interval** (300 seconds by default, per pod): the first alert of a type is sent at once, further alerts of the same type inside the interval are counted, and the count is added to the next alert of that type. This replaces the original wording "one email per error", which would send one email per failed request during an outage (owner decision of 2026-10-13, [ADR 0004](adr/0004-alert-throttling.md)). Invalid requests, missing tokens and unknown routes never notify. The email subscription must be confirmed once by the owner, or no alarm, alert, AWS Health notice or reminder is delivered.

## CSV ingestion

The owner drops a `.csv` file in `incoming/` of the ingestion bucket. S3 puts an event on an SQS queue and the ingestion worker (one pod) reads it.

- Header: `operation,operand_a,operand_b,occurred_at` (`add`, `sub`, `mul`, `div`; ISO-8601 timestamp in UTC, `Z` or `+00:00`). Maximum 5 MB. The object key must be `incoming/<name>.csv` with a plain file name (letters, digits, `.`, `_`, `-`); any other key is ignored and reported.
- Each valid row is saved with an id derived from its content, so ingesting a file twice creates no duplicates. Rows are saved in batches of 500.
- Invalid rows (unknown operation, bad number, out of range, division by zero, bad timestamp, wrong column count) are skipped and listed with line and reason in `reports/<name>.csv.txt` (at most 1000 are listed).
- When processing ends the file moves to `processed/`. A file that is not UTF-8 CSV, has a wrong header or is larger than 5 MB moves to `rejected/` with a report in `reports/` and an email.
- An infrastructure failure (database, S3) leaves the file in `incoming/` and the message returns to the queue; after 3 receives it goes to the dead-letter queue, an alarm sends an email, and the owner decides what to do with the file.
- Retention: `incoming/` 30 days, `processed/` and `rejected/` 30 days in `dev` and 90 in `prod`, `reports/` 30 days in `dev` and 365 in `prod`.

## Behaviour of the environment

- **Night window (dev).** The NAT gateway is removed from 22:00 to 08:00 Panama time. While it is down the pods keep answering requests through the load balancer, but a new pod or node, a new image, a secret read after a credential rotation, an alert email, an autoscaler action, a bastion session and a deployment can fail or wait until 08:00. `Keep NAT` (a workflow) skips the shutdown on demand. `prod` has no schedule.
- **Credentials.** Pods use a limited database user (read and insert on the calculator tables) whose secret rotates every 3 days between 09:00 and 17:00 Panama time. The pods fetch the new password on an authentication failure and retry; no request should fail.
- **Scaling.** The five HTTP workloads scale between 1 and 4 replicas (`prod`: 2 and 4) when they average more than 50 % of the requested CPU, and back after 5 minutes of low use. The node group scales between 2 and 4 nodes when pods cannot be scheduled and back after about 10 minutes of low use. Custom metrics (requests per second, queue length) are out of scope.
- **Monitoring.** Emails for: no healthy target of a workload, fewer nodes in service than the minimum, load balancer or target 5XX, database storage, connections, CPU and memory low or high, proxy authentication and database errors, messages in the dead-letter queue, oldest queue message too old, failed deployment builds, failed secret rotation and failed NAT scheduler runs (dev). AWS Health events for EKS arrive by email, and a weekly reminder (the first one 7 days after creation) states the cluster name, version and the date its support ends, so it is upgraded or destroyed in time.
- **Logs.** Structured JSON on standard output (see [technical.md](technical.md) for the messages). Pod logs are read from the bastion; they are not shipped to CloudWatch.

## Moving traffic between backends

Weights decide the backend. They are GitHub environment variables `API_WEIGHT_<NAME>` of `shared-resources` (for example `API_WEIGHT_EKS` 100 and the others 0); a change needs a deployment of that repository and is reversible by changing the weights again. After every deployment or recreation of this backend the load balancer name changes, and the deployment summary reminds the owner to run `Deploy DEV` in `shared-resources` so Route 53 reads the new registration. To test this backend before moving traffic, resolve the API host name to the load balancer address on the tester's machine only (for example `curl --resolve <api host>:443:<load balancer ip>`), so the shared certificate still matches. The web page shows `Backend: eks` and the response time. See [technical.md](technical.md), "Shared edge registration".

## Retirement of the environment

Do the steps in this order; the detail is in [technical.md](technical.md), "Destroy order".

1. In `shared-resources` set the weight of `eks` to 0 (and another backend above 0) and deploy it. Never destroy a backend that still receives traffic.
2. Run `Destroy DEV` in `plan` mode, read the list, then in `destroy` mode typing the repository name. It uninstalls the Helm releases, waits until the load balancer is gone, removes the registration parameters, deletes the stacks in reverse order and runs the leftover check.
3. Deploy `shared-resources` once more (or wait for its daily drift check) so the weighted record of `eks` is dropped.
4. `prod` has no destroy workflow: lift deletion protection and destroy by hand with a typed confirmation, after reading the runbook.
