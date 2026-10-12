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
  render `namespace.yaml`, `serviceaccounts.yaml` and `migrate-job.yaml` with `--show-only`; pass `--set migrate.enabled=false`
  to the release so the Job is not created twice.
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
- **Network policy.** The ALB (IP mode) connects from the addresses of its network interfaces in the VPC, which no
  pod or namespace selector can name: ingress to the API pods is allowed from the VPC CIDR (`ipBlock`) on 8443; the
  security groups (only the load balancer group reaches the pod port) narrow it. `allow-pod-identity-egress`
  (169.254.170.23:80) is needed so that pods can obtain their pod identity credentials.
