# calculator chart

Workloads, policies and the Ingress group of the calculator backend (release `calculator`, namespace `calculator`).
Installed by `scripts/helm_deploy.sh` (task 4.7) from the verified source archive.

```
helm template calculator chart -f chart/values-dev.yaml --namespace calculator \
  --set-string image.repository=... --set-string image.digest=sha256:... \
  --set-string app.alertsTopicArn=... --set-string app.appSecretArn=... --set-string app.masterSecretArn=... \
  --set-string app.dbHost=... --set-string app.dbInstanceHost=... \
  --set-string ingress.certificateArn=... --set-string ingress.securityGroupId=... \
  --set-string ingress.accessLogs.bucket=... (optional) --set-string auth.userPoolId=... --set-string auth.appClientId=...
```

Prod also needs `ingress.wafAclArn` (the render stops without it). Tests: `python -m pytest chart/tests -q`
(render tests are skipped when `helm` is not on PATH).

## Notes

- **Access logs.** `ingress.accessLogs.bucket` is optional: when it is empty the load balancer gets no access-log
  attributes (only `drop_invalid_header_fields`). The log bucket belongs to the storage stack (task 5.1), so the
  first deployments run without it; the deployment script passes the bucket once the stack exists.

- **Namespace.** Helm cannot store the release in a namespace that does not exist, and `--create-namespace` creates one
  the release cannot adopt. Apply `helm template --show-only templates/namespace.yaml` (it carries the Helm ownership
  metadata) before `helm upgrade --install`; the release then adopts it. `namespace.create=false` skips it.
- **Migration.** `templates/migrate-job.yaml` renders one Job named `migrate-<migrate.runId>` (release revision when
  empty). It needs the namespace and the `migrate` service account, so it is not a hook. To run it before the release,
  render `namespace.yaml`, `serviceaccounts.yaml`, `migrate-networkpolicy.yaml` and `migrate-job.yaml` with `--show-only`;
  pass `--set migrate.enabled=false` to the release so the Job is not created twice. `migrate-networkpolicy.yaml` is not
  conditional on `migrate.enabled`: the release keeps the egress of the Job next to `default-deny-all` and adopts it.
- **IngressGroup and authentication.** Two Ingress objects share `group.name`. `jwt-validation` is read from the
  annotations of each Ingress and applied only to the rules of that Ingress (controller v3.6.0, annotation table:
  merge behaviour "N/A", not Exclusive), so one object can be authenticated and the other not. Annotations that
  configure the load balancer or listeners are Exclusive: both objects carry identical text. The preflight object has
  `group.order` 10, the authenticated one 20 (smaller is evaluated first).
- **OPTIONS-only rule.** The unauthenticated Ingress adds `conditions.<service>` with `http-request-method` =
  `OPTIONS` for every service it routes to (the annotation name must equal the service name). The authenticated Ingress
  has no method condition, so every other method on the same paths hits `jwt-validation`; `pathType: Exact` leaves
  anything else to the listener default.
- **Default action.** No Ingress sets `spec.defaultBackend` (two would fail the group); the controller then builds a
  fixed response 404 for the listener by itself. HTTP 80 redirects to 443 (`ssl-redirect`).
- **Limitations.** `jwt-validation` works on HTTPS listeners only; a rejected token gets the ALB 401 which cannot be
  customised (gap G5). Each Ingress gets its own target group per service (10 target groups, 20 listener rules).
  Any user who can create Ingress objects with the same `group.name` can add rules to the ALB: restrict RBAC.
- **Network policy.** Default deny plus one policy per workload (`add`, `sub`, `mul`, `div`, `history`, `ingest`, `migrate`).
  The ALB (IP mode) connects from the addresses of its network interfaces, which no pod selector can name: the API pods
  accept ingress on 8443 only from the subnets of the load balancer (`network.albCidrs`), never from the whole VPC or the
  pod subnets, so a compromised pod cannot skip the JWT validation of the ALB. `ingest` and `migrate` accept no ingress.
  Each policy lists its own egress (DNS to CoreDNS, the database subnets on 5432, HTTPS to AWS APIs, and the pod identity
  agent 169.254.170.23:80, which pods need to obtain their credentials).
- **Environment and logging.** Every pod and the migrate job get `ENVIRONMENT` (= `envType`) and `LOG_LEVEL` (= `app.logLevel`, `INFO` in dev and prod). DEBUG only as an explicit, temporary override (`--set-string app.logLevel=DEBUG`); the application keeps `botocore`, `boto3`, `urllib3`, `s3transfer` and `psycopg` at WARNING even then.
- **Database trust file.** `app.sslRootCert` (`/app/db-trust-bundle.pem`) holds the RDS global bundle plus the Amazon Root CA 1 to 4: RDS Proxy presents an ACM certificate, which does not chain to the RDS CAs.
