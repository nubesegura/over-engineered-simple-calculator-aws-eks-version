# Technical documentation

Related: [functional.md](functional.md) (what the product does), [architecture.md](architecture.md) (components, resource inventory, decisions), [adr/](adr/README.md).

## Stack

- Python 3.14, Starlette 1.7 and Uvicorn 0.54, psycopg 3 with `psycopg-pool`, boto3, `cryptography`. Every version is pinned with `==` (transitives included) in `src/backend/calculator_core/requirements*.txt`; Dependabot proposes updates weekly ([ADR 0009](adr/0009-dependabot-weekly.md)).
- Hexagonal package `src/backend/calculator_core/` (`domain`, `application` use cases and ports, `adapters` inbound and outbound, `config` as the only composition root). `.importlinter` keeps the layers apart and keeps boto3, the web framework and the database driver out of `domain` and `application`.
- One image, `src/backend/Dockerfile`, started as `calculator <add|sub|mul|div|history|ingest|migrate>`: base `python:3.14-slim-trixie` pinned by digest (arm64), non-root user 10001, code, trust file and migrations read-only, `/tmp` the only writable path ([ADR 0015](adr/0015-one-image-deployed-by-digest.md), [ADR 0007](adr/0007-base-image-and-autoscaler-pinning.md)).
- Infrastructure: AWS SAM / CloudFormation, 11 templates in `templates/` (stack order and `onlyIf` rules in `templates/stacks.json`), one parameter file per environment in `parameters/{dev,prod}.json` (both must keep the same keys). Kubernetes resources: Helm chart `chart/` with `values.yaml`, `values-dev.yaml`, `values-prod.yaml`; controller values in `chart-addons/`. Region `us-east-2`, one AWS account per environment. The resource inventory is in [architecture.md](architecture.md).
- Names are `<acronym>-useast2-oecalc[-eks][-<descriptor>]-<env>`; stacks are `stk-useast2-oecalc-eks-<unit>-<env>`; data crosses stacks as exports named `<stack>:<Output>`. The five mandatory tags (`team-owner`, `project-name`, `app-name`, `repo-name`, `env-type`) are in the parameter files, passed as stack tags, and given to the controller as default tags.

## Environments

| | `dev` | `prod` |
|---|---|---|
| Deployed | Yes, by `Deploy DEV` (manual dispatch) | No; designed and checked offline. `Deploy PROD` on push to `main` |
| VPC | 10.30.0.0/16 | 10.31.0.0/16 |
| NAT | One gateway, removed 22:00 to 08:00 Panama by the scheduler (stack `scheduler`) | One per zone, always on, owned by the network stack |
| Database | Single-AZ, 1 day of backups, no deletion protection, no final snapshot, engine log export off | Multi-AZ, 15 days, deletion protection, final snapshot, log export on (a template rule refuses a parameter file that turns these off) |
| Cluster | Kubernetes 1.37, 2 to 4 `t4g.medium` nodes | Same, deletion protection on |
| Replicas | HPA 1 to 4 | HPA 2 to 4 and PodDisruptionBudgets |
| Extras | Bastion (Session Manager), scheduler | Flow logs, WAF ACL (`WAF_ACL_ARN`) |
| Log retention | 14 days | 90 days |
| Controller replicas | 1 | 2 |
| Cluster Autoscaler | Installed with the accepted minor skew | The install stops the deployment until the pinned version matches (ADR 0007) |

## Services

Each HTTP workload mounts only its routes under `/api/v1` and `/api/eks/v1`, plus `/health`, and listens on TCP 8443 over HTTPS with a throwaway self-signed certificate generated at start (`adapters/inbound/self_signed_tls.py`, written to `/tmp`; the load balancer does not validate target certificates). One ASGI middleware (`adapters/inbound/http_app.py`) handles the request id (the trace root of the load balancer when present), the CORS preflight, the allowed-origin header on every response, error translation and the access log. The service never verifies the token; it only decodes `sub` for the log.

Configuration is read from environment variables and validated at start; a missing or invalid value stops the start with a message that names the variables, never their values.

| Variable | Used by | Meaning |
|---|---|---|
| `AWS_REGION`, `ENVIRONMENT`, `LOG_LEVEL` (default `INFO`) | all | Region, `dev` or `prod`, level of the application loggers |
| `ALERTS_TOPIC_ARN` | all | SNS topic of the alerts |
| `APP_SECRET_ARN` | all | Secret of the limited database user (read through Secrets Manager, never in an environment variable) |
| `DB_HOST`, `DB_PORT` (5432), `DB_NAME` | HTTP, worker | RDS Proxy endpoint and database |
| `SSL_ROOT_CERT` | all | Trust file, `/app/db-trust-bundle.pem` |
| `ALLOWED_ORIGIN` | HTTP | CORS origin; an explicit origin, never `*` |
| `CURSOR_KEY_SECRET_ARN` | `history` | Secret with the cursor signing key |
| `INGEST_QUEUE_URL`, `INGEST_BUCKET` | `ingest` | Queue and bucket |
| `DB_CONNECT_TIMEOUT_SECONDS` (3), `DB_POOL_TIMEOUT_SECONDS` (3), `DB_POOL_MAX_WAITING` (20) | HTTP, worker | Bounded waits ([ADR 0006](adr/0006-bounded-waits-and-timeouts.md)) |
| `ALERT_THROTTLE_SECONDS` (300; 0 sends every alert) | HTTP, worker | One alert per error type per interval ([ADR 0004](adr/0004-alert-throttling.md)) |
| `INGEST_BATCH_SIZE` (500, 1 to 5000) | `ingest` | Rows per transaction ([ADR 0005](adr/0005-ingestion-batching.md)) |
| `MASTER_SECRET_ARN`, `DB_INSTANCE_HOST`, `MIGRATIONS_DIRECTORY` | `migrate` | Master secret, direct instance endpoint, SQL files |

The chart sets these from stack outputs; none is a secret. The pods fetch the database credentials with their Pod Identity role and reconnect with the refreshed password when an authentication failure follows a rotation.

## Database

PostgreSQL 17 on RDS (`db.t4g.micro`, gp3 20 GB, CMK, `rds.force_ssl=1`, private subnets). Schema through versioned SQL in `src/backend/migrations/` (`0001_create_calculations.sql`: table `calculations` and the history index; `0002_group_role.sql`: group role `calc_app_rw`, which cannot log in and holds `SELECT` and `INSERT` on the table). The `migrate` command applies them with the master user straight to the instance, creates the login user `calc_app` as a member of the group role and runs once per deployment as a Kubernetes Job before the release is touched; a failed migration stops the deployment.

Identities: the master user `calcadmin` (secret managed and rotated by RDS) is read only by the migration job, the rotation function and account administrators. Services and the worker use `calc_app` through the RDS Proxy over TLS. Its secret rotates every 3 days with the alternating-user strategy (`calc_app` and `calc_app_clone`), in a window of 8 hours starting 14:00 UTC; the function runs in the private subnets and connects to the instance. The proxy has TLS required, IAM database authentication off and the application secret registered.

### Certificate rule for the RDS Proxy

The clients verify the server certificate (`verify-full`). The two kinds of endpoint are signed by different authorities:

| Endpoint | Used by | Signed by | Trust file |
|---|---|---|---|
| RDS Proxy | HTTP workloads, ingestion worker | AWS Certificate Manager, chaining to the Amazon Trust Services roots (Amazon Root CA 1 to 4) | `/app/db-trust-bundle.pem` in the image |
| DB instance | migration job (image), rotation function | RDS certificate authorities | the same image file (RDS bundle part); the RDS bundle packaged with the function |

The image file holds the RDS global bundle followed by the four Amazon roots (pinned by SHA-256 in `src/rotation/package_bundle.py`, validated at build). **Rule:** whenever a client or an endpoint is added, state which endpoint it uses and which authority signs it, and make sure its trust file holds that authority ([ADR 0001](adr/0001-rds-proxy-certificate-trust.md)). The symptom of a missing root is every database request failing after about 10 seconds (3 seconds now) with `certificate verify failed` while the targets stay healthy, because `/health` does not use the database.

## Edge

The ALB listens on 443 (policy `ELBSecurityPolicy-TLS13-1-2-2021-06`, certificate read by the deployment from the SSM parameter `/oecalc/<env>/api-certificate-arn` published by `shared-resources`; the deployment stops when it is empty). HTTP on 80 redirects to 443. Two Ingress objects in one group (`calculator-preflight`, `calculator-api`) give each service a preflight rule (method `OPTIONS`, no token) and a rule with `jwt-validation`; paths are exact; the listener default is a fixed 404; there is no `/health` route ([ADR 0018](adr/0018-token-validation-at-the-load-balancer.md)). The ALB security group has egress 443 to reach the pool keys; without it the rule returns an error produced by the load balancer. Access logs go to the ALB log bucket, and invalid header fields are dropped. In `prod` the regional WAF ACL (`WAF_ACL_ARN`) is attached; the chart refuses `prod` without it.

## Network policies

The namespace `calculator` enforces Pod Security `restricted` and starts from a default deny. Each workload has its own policy, `<workload>-network` ([ADR 0002](adr/0002-per-workload-network-policies.md), [ADR 0003](adr/0003-alb-only-ingress-cidrs.md)):

- Ingress (HTTP workloads only): TCP 8443 from the two public subnets (`network.albCidrs`).
- Egress: DNS to CoreDNS; TCP 5432 to the two private database subnets (`network.databaseCidrs`); TCP 443 to anything outside the VPC range; TCP 80 to the Pod Identity agent at 169.254.170.23.
- The ingestion worker and the migration job have no ingress.

The CIDR values in `chart/values-<env>.yaml` must match the subnet plan in `templates/network.yaml`; a chart test checks them against `parameters/<env>.json`.

## Delivery

Workflows in `.github/workflows/`:

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yml` | pull request, push, called by the deploys | `iac` (cfn-lint, `sam validate --lint`, script and template tests, scheduler tests), rotation and scheduler checks, `python-core` (ruff, mypy strict, import-linter, pytest with coverage of at least 85 percent and a PostgreSQL service, `pip check`, `pip-audit`), `chart` (helm lint and render tests for both environments), `image` (arm64 build under emulation, uid check, dispatcher, `/health`) |
| `deploy-dev.yml` | manual dispatch (input `ignore_nat_window`) | CI, then `deploy.yml` for `dev` |
| `deploy-prod.yml` | push to `main`, or manual from `main` | CI, a dry-run plan, then the real deployment ([ADR 0008](adr/0008-production-path-on-main-with-dry-run.md)) |
| `deploy.yml` | called by the two above | The deployment itself |
| `destroy-dev.yml` | manual dispatch | `Destroy DEV` (plan or destroy) |
| `keep-nat.yml` | manual dispatch | Writes the SSM parameter `/oecalc/dev/eks/nat-keep-up` |

Every workflow, job, step and scenario as an interactive pipeline (open in a browser; works offline): [workflows-flow-animation.html](workflows-flow-animation.html).

Steps of `deploy.yml`: refuse `prod` outside `main`; validate the environment variables and secrets; verify the AWS account; check the NAT window (dev) and the IaC owner guard (fails closed if Terraform owns the environment); package the rotation function; in plan mode check the Kubernetes version and create the change sets of `security` through `storage` without executing them; otherwise deploy those stacks with change sets (a removal or a replacement stops the run, except the units' allowed exceptions in `stacks.json`; the VPC default security group is emptied after the network stack); start the CodeBuild project with `DEPLOY_COMMAND=addons` (AWS Load Balancer Controller 3.6.0 and Cluster Autoscaler 9.59.0, chart archives checked by SHA-256, the read-only cluster role for the bastion); build the image (arm64, immutable tag `<commit>-<run id>`) and read its digest from the registry; start the project with `DEPLOY_COMMAND=deploy` (applies the namespace, service accounts, the migration network policy and the migration Job, waits for the Job, `helm upgrade --install --atomic --wait`, waits for every rollout and for the load balancer address, publishes the registration parameters); discover the alarm dimensions by tag (node group, load balancer, target groups) and enable the Auto Scaling group metric; deploy the `alarms` stack last; print the summary with the next steps. A failed Helm upgrade is rolled back to the previous release and the build fails with the cause.

GitHub environment (`dev`, `prod`): secrets `ROLE_ARN`, `AWS_ACCOUNT_ID`, `SUPPORT_EMAIL`; variables `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` and, in `prod`, `WAF_ACL_ARN`. AWS access is by OIDC with the role `github-actions-terraform-<env>-role`. The workflow stops before touching AWS if one is missing. There are no required approvers (single-user organization): the safety is the plan or dry run, the change set guard, the branch rule and the typed confirmation of the destroy.

Image build (run from the repository root): `docker buildx build --platform linux/arm64 --build-context rotation=src/rotation -f src/backend/Dockerfile --tag <repository>:<tag> src/backend`.

## Logs

Structured JSON on standard output, one object per line with `timestamp`, `level`, `logger`, `message`, `service` (`calculator-add`, ..., `calculator-ingest`), `environment`, `correlation_id` (the request id), `sub` (the caller, decoded without verification) and, for exceptions, `exception_type`, `cause_type`, `aws_error` and the stack frames (file, line, function; never the exception message). `/health` writes nothing.

| Level | Message | When |
|---|---|---|
| INFO | `Request received: method=%s path=%s request_id=%s body=%r` (`query=%r` for history) | Every API request; body and query cut at 2000 characters; a body larger than 4096 bytes is not read |
| INFO | `Response sent: status=%d request_id=%s body=%s` | Every API response, cut at 2000 characters |
| INFO | `<method> <path> <status> <ms>ms` (logger `calculator_core.access`) | Every request |
| WARNING | `Request rejected: code=%s reason=%s request_id=%s received=%r` | Any 400 |
| ERROR | `Internal error while processing the request: ...` with the exception fields | Any 500 |
| ERROR | `The error notification failed (<type>).` | The alert could not be published |
| WARNING / ERROR | `The queue could not be read; retrying shortly.`, `A file could not be processed; the message stays in the queue.`, `A queue message was not an S3 event notification and was dropped.` | Ingestion worker |

Never logged: request headers, the `Authorization` value, tokens, secrets, passwords, exception messages of drivers and SDKs. The body is logged (two numbers and an operation); revisit this if the API ever accepts free text. The loggers of `botocore`, `boto3`, `urllib3`, `s3transfer` and `psycopg` stay at WARNING ([ADR 0012](adr/0012-log-level-cap-and-redaction.md)). Pod logs are not shipped to CloudWatch (backlog); the control plane, RDS proxy, CodeBuild, Lambda and session logs are.

## Runbooks

Account and region first: `dev` account, `us-east-2`. Commands marked "operator machine" run with read-only AWS access from your computer; commands marked "bastion" run inside a Session Manager session. Replace `<...>`; nothing here needs a secret value.

### Bastion access

The bastion exists in `dev` only. It has no inbound rule, no public IP and no key pair, and its cluster access is read-only (pods, events, logs, nodes, services, ingresses; no Secrets, no changes).

```
# operator machine (needs the Session Manager plugin and permission to start a session)
aws cloudformation describe-stacks --stack-name stk-useast2-oecalc-eks-bastion-dev --region us-east-2 \
  --query "Stacks[0].Outputs[?OutputKey=='BastionInstanceId'].OutputValue" --output text
aws ssm start-session --region us-east-2 --target <instance id> --document-name ssmd-useast2-oecalc-eks-bastion-dev
```

The document logs the session to `/aws/ssm/session/useast2-oecalc-eks-bastion-dev` and ends it after 20 idle minutes. `kubectl`, `helm`, the AWS CLI, `k9s`, `stern` and `jq` are installed at the first boot and `KUBECONFIG` is set. If a tool is missing, read `/var/log/oecalc-bastion-init.log`: the downloads need the NAT gateway, so an instance created between 22:00 and 08:00 must be recreated. To allow edits in the `calculator` namespace set the parameter `BastionWriteAccess` to `true` on purpose; the default stays `false`.

### Read pod logs and state (bastion)

```
kubectl get nodes
kubectl get pods -n calculator -o wide
kubectl get events -n calculator --sort-by=.lastTimestamp
kubectl logs -n calculator deploy/add --tail=100            # also sub, mul, div, history, ingest
kubectl logs -n calculator deploy/add --since=10m | jq 'select(.level=="ERROR")'
kubectl logs -n calculator -l app.kubernetes.io/name=history --tail=50 --previous   # crashed container
stern -n calculator '.*' --since 5m                         # all workloads at once
k9s -n calculator
kubectl logs -n calculator job/migrate-r<run id>            # the migration of a deployment
kubectl get hpa,pdb,networkpolicy -n calculator
kubectl top pods -n calculator ; kubectl top nodes
```

Follow one request: take the `request_id` from the error body (or `correlation_id` in the logs) and filter every workload: `stern -n calculator '.*' --since 15m | grep <request id>`. Secrets cannot be read from the bastion, by design.

### Debugging path: DNS, load balancer, pod, database

Work from the outside in and stop at the first layer that fails.

1. **DNS and the registration** (operator machine). The API host name resolves to the load balancer of the backend that has weight. Check that the name Route 53 uses is the current load balancer:
   ```
   dig +short <api host name>
   aws ssm get-parameter --region us-east-2 --name /oecalc/dev/api-backends/eks/dns-name --query Parameter.Value --output text
   kubectl get ingress calculator-api -n calculator -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'   # bastion
   ```
   If the parameter and the ingress address differ, or the name does not resolve to the load balancer, run `Deploy DEV` in `shared-resources` (the address changes every time the cluster is recreated). If the weight of `eks` is 0 the name points elsewhere on purpose.
2. **Load balancer** (operator machine). An unauthenticated request must get `401` from the load balancer and a preflight `204`:
   ```
   curl -si https://<api host name>/api/v1/history | head -n 5
   curl -si -X OPTIONS https://<api host name>/api/v1/add -H 'Origin: <web origin>' -H 'Access-Control-Request-Method: POST' | head -n 8
   aws elbv2 describe-load-balancers --region us-east-2 --query "LoadBalancers[?starts_with(LoadBalancerName,'k8s-')].[LoadBalancerName,State.Code,DNSName]"
   aws elbv2 describe-target-groups --region us-east-2 --load-balancer-arn <load balancer arn> --query "TargetGroups[].[TargetGroupName,TargetGroupArn]"
   aws elbv2 describe-target-health --region us-east-2 --target-group-arn <target group arn> --query "TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]"
   ```
   A `401` with a valid token: it must be the ID token (not the access token), not expired, with `aud` equal to the app client of the environment. An error produced by the load balancer with a valid token: the ALB security group lost its egress 443 to the pool keys. Unhealthy targets: go to step 3. Access logs are in the ALB log bucket (`bckt-useast2-oecalc-eks-alb-logs-<account id>-dev`, prefix `alb`).
3. **Pod** (bastion). Targets are pod IPs (`target-type: ip`):
   ```
   kubectl get pods -n calculator -o wide                      # Running, READY 1/1, restarts
   kubectl describe pod -n calculator <pod>                    # probes, events, Pending reasons
   kubectl get endpoints -n calculator add
   kubectl logs -n calculator deploy/add --tail=100
   ```
   `Pending` pods: quota or no capacity (`kubectl describe`; the autoscaler needs the NAT gateway). `ImagePullBackOff` or `ErrImagePull`: the NAT gateway is down or the digest does not exist. `CrashLoopBackOff`: read `--previous`; a settings error names the variables. Ready pods that the target group calls unhealthy: the pod port or the network policy for the load balancer subnets (`kubectl get networkpolicy -n calculator add-network -o yaml`).
4. **Database** (a 500 with healthy targets points here, because `/health` never touches the database).
   ```
   kubectl logs -n calculator deploy/add --tail=50 | jq 'select(.level=="ERROR")'    # bastion: exception_type, cause_type
   aws rds describe-db-proxies --region us-east-2 --db-proxy-name rdsp-useast2-oecalc-eks-dev --query "DBProxies[0].Status"
   aws rds describe-db-proxy-targets --region us-east-2 --db-proxy-name rdsp-useast2-oecalc-eks-dev --query "Targets[].TargetHealth"
   aws rds describe-db-instances --region us-east-2 --db-instance-identifier rds-useast2-oecalc-eks-dev --query "DBInstances[0].DBInstanceStatus"
   aws secretsmanager describe-secret --region us-east-2 --secret-id sm-useast2-oecalc-eks-app-dev --query "[RotationEnabled,LastRotatedDate]"
   ```
   `certificate verify failed`: the trust file lacks the signer of the endpoint (certificate rule above). Authentication errors right after a rotation: the proxy and the secret disagree; check the alarms `proxy-auth-errors` and `db-rotation-errors`. `PoolTimeout` or timeouts: the proxy or database is unreachable (security group, outage) or saturated (`rds-connections` alarm). Secret values are never read in this procedure.
5. **Alarms and alerts.** Every step above has an alarm; the email arrives only if the subscription is confirmed (SNS console or `aws sns list-subscriptions-by-topic`, status `Confirmed`).

### Shared edge registration and retiring a backend

Registration: after the rollout, `scripts/helm_deploy.sh` publishes `/oecalc/<env>/api-backends/eks/dns-name` (load balancer DNS name) and `.../hosted-zone-id` (its canonical hosted zone id), tagged with the five tags; only the CodeBuild role may write that path. `shared-resources` reads every published backend and creates one weighted alias record per backend from the variables `API_WEIGHT_<NAME>` (0 to 255; a plan where every published backend has weight 0 is refused).

To start: deploy this repository, run `Deploy DEV` in `shared-resources`, then set `API_WEIGHT_EKS` and deploy it again. To change traffic: change the weights, deploy `shared-resources`. After this repository is recreated the load balancer name changes: deploy `shared-resources` again.

To retire the backend: set another backend's weight above 0 and `API_WEIGHT_EKS` to 0 and deploy `shared-resources`; destroy this environment (parameters disappear); deploy `shared-resources` again, or wait for its daily drift check (`drift-check.yml`, plan only) to flag it. The module drops the weighted record and its delete guard allows exactly that delete. `Destroy DEV` prints this as its closing notice.

### Destroy order

`Destroy DEV` (mode `plan` is the default, nothing is touched; mode `destroy` needs the repository name typed in `confirm`; it runs only from `develop` and only against `dev`). Inside `scripts/cfn_deploy.py destroy`:

1. The owner guard detects who owns the environment: `cloudformation` continues, `none` ends green, `terraform`, both or unreadable fail closed.
2. The plan lists every stack in reverse order with its status, what it would retain and what blocks a delete.
3. When the cluster and the deployment stack are usable, the scheduler function brings the NAT gateway back if it is absent (the uninstall build downloads `kubectl` and `helm`); it is removed again when the scheduler stack is deleted.
4. Helm uninstall through CodeBuild, in this order: Ingress objects (the controller removes the load balancer, target groups and its security groups while it still runs), the `calculator` release and its namespace, left-over target group bindings, then `cluster-autoscaler` and `aws-load-balancer-controller`.
5. The script waits, read-only, until the load balancer, target groups and controller security groups are gone, then removes the SSM registration parameters.
6. Stacks in reverse order (`alarms`, `storage`, `bastion`, `deploy`, `cluster`, `rotation`, `database`, `scheduler`, `registry`, `network`, `security`): termination protection off, empty only this project's buckets and repositories, delete, wait. A `DELETE_FAILED` stops the run with the failed events. The scheduler removes the NAT gateway and its Elastic IP.
7. `scripts/post_destroy_check.py` lists what is left (by tags and by naming convention) and fails if something unexpected remains; KMS keys pending deletion (7 days in `dev`) and secrets scheduled for deletion are labelled as expected residues.
8. Closing notice: deploy `shared-resources` so Route 53 drops the record, and set `API_WEIGHT_EKS` to 0.

`prod` has no destroy workflow. Before a manual destroy: set the database deletion protection and the cluster deletion protection off, expect a final snapshot, and note that the buckets and repositories are not emptied automatically when `ForceDestroyData` is `false`. The same lift-protection step will be needed for the load balancer when its deletion protection is added (open item).

### Operating the environment

- **NAT window.** `Keep NAT` with `keep: true` leaves the gateway up past 22:00 (it stays up until you run it again with `false`); manual equivalent: `aws ssm put-parameter --name /oecalc/dev/eks/nat-keep-up --type String --overwrite --value true`. Deploy and run load tests between 08:00 and about 20:30 Panama time; `ignore_nat_window` overrides the deployment check.
- **Load test** (bastion, during the day): `scripts/loadtest/run.sh --url https://<api host name> --token-file ~/id-token.txt --duration 300 --concurrency 20`. The token is a Cognito ID token placed in a private file for the session (delete it afterwards); it is never an argument and is never printed. Watch in other sessions `kubectl get hpa -n calculator -w`, `kubectl get pods -n calculator -o wide -w` and `kubectl get nodes -w`. Expected: replicas above 1 up to 4, new nodes up to 4 within minutes, return to 1 replica after the 5 minute stabilization window and to 2 nodes after about 10 minutes of low use. Details in `scripts/loadtest/README.md`.
- **Ingestion check.** Upload a valid, an invalid and a repeated CSV to `incoming/`, then list `processed/`, `rejected/` and `reports/` and confirm there are no duplicate rows in the history. A message in the dead-letter queue means an infrastructure failure: read the worker log, fix the cause and redrive or remove the message.
- **Alert subscription.** After the first deployment, confirm the email subscription of the alerts topic once.
- **Upgrade or recreate.** The weekly reminder gives the end-of-support date. Newer Kubernetes versions require the add-on versions in `parameters/<env>.json`, the pinned `kubectl` and `helm`, and the autoscaler to match (ADR 0007).

### Production checklist (first deployment, owner)

1. GitHub environment `prod`: the variables and secrets listed under Delivery, including `WAF_ACL_ARN`.
2. Create the Auto Scaling service-linked role once (`aws iam create-service-linked-role --aws-service-name autoscaling.amazonaws.com`) and set `AutoScalingServiceLinkedRoleExists` to `true`.
3. Decide the autoscaler version (ADR 0007); size the alarm thresholds and the namespace quota.
4. Merge `develop` into `main`: read the plan job, which creates nothing; the apply job follows.
5. Open items in [architecture.md](architecture.md), "Known limits", before relying on it.

## Local checks

From the repository root:

- Infrastructure and scripts: `pip install -r scripts/requirements-dev.txt cfn-lint==1.48.1`, `cfn-lint --regions us-east-2 -- templates/*.yaml`, `python -m pytest scripts/tests`.
- Chart: `helm lint chart -f chart/values-dev.yaml` (and `values-prod.yaml`), `python -m pytest chart/tests -q` (render tests need `helm` on the path).
- Backend, from `src/backend/calculator_core`: `ruff check .`, `ruff format --check .`, `mypy`, `lint-imports`, `python -m pytest` (the PostgreSQL tests run when `TEST_DATABASE_URL` is set; CI runs them against a PostgreSQL service).

## Known exceptions (owner-accepted)

Alarm and schedule names ([ADR 0011](adr/0011-alarm-and-schedule-name-deviation.md)), security groups for pods ([ADR 0010](adr/0010-no-security-groups-for-pods.md)), KMS key policies that trust the account root, no pod log shipping, no image signing, no WAF in `dev`, Cluster Autoscaler one minor behind in `dev`, tokens verified only at the load balancer. The full list with the open items is in [architecture.md](architecture.md).

## Operational lessons

- A health check that does not use the database cannot detect a broken database path; accept a first deployment only after a real write through the proxy.
- Renaming or changing a force-new property (security group description, alarm name, schedule name) replaces the resource; read every change set for replacements. The deployment script stops on removals and replacements.
- Security group names cannot start with `sg-`: the acronym is `sgp`.
- Library log levels must never follow `LOG_LEVEL` below WARNING.
- The `ecs` and `sls` repositories deploy on pushes to `develop`; check their workflow `paths` before pushing there.
- The load balancer address changes with every recreation: the shared edge must be deployed again.
