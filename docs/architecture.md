# Architecture

> Status: `dev` is deployed and verified; `prod` is designed to the same templates and chart, checked offline and never deployed.
> Written at the closing of spec `001-create-baseline` against the real templates (`templates/*.yaml`), the chart (`chart/`) and the scripts.
>
> Diagrams (regenerated from the implemented IaC at closing; the pictures are exported with File > Export as > PNG, 2x):
> - AWS components: [architecture.drawio](architecture.drawio), [architecture.png](architecture.png)
> - Layers (edge, compute, data, operations): [layers.drawio](layers.drawio), [layers.png](layers.png)
>
> Related documents: [functional.md](functional.md) (what the product does), [technical.md](technical.md) (configuration, deployment, runbooks), [adr/](adr/README.md) (decisions).

## Overview

```
Browser --> Route 53 (weights, shared-resources) --> ALB (443, JWT validation) --> pods (EKS managed node group, private subnets)
                                                         |                           |--> RDS Proxy --> RDS PostgreSQL
                                                         +--> Cognito JWKS (443)     +--> NAT (08:00-22:00 in dev) --> ECR, Secrets Manager, SNS, STS
S3 incoming/*.csv --> SQS (+DLQ) --> ingestion worker (pod) --> RDS Proxy
GitHub Actions --OIDC--> CodeBuild (in the VPC) --> private EKS API (helm, kubectl)        Owner --Session Manager--> bastion (read-only kubectl)
```

Ownership: CloudFormation/SAM owns the network, keys, database, cluster, roles, storage, alarms and the CodeBuild project; Helm owns the workloads, policies and the Ingress; the AWS Load Balancer Controller owns the load balancer, listeners, rules, target groups and the controller security groups. The shared API edge (hostname, certificate, weighted DNS records) belongs to `over-engineered-simple-calculator-shared-resources` and Cognito to `over-engineered-simple-calculator-webpage`; this repository reads them and registers itself as the `eks` backend.

## Design decisions

| ID | Decision | Reason |
|---|---|---|
| D1 | Kubernetes 1.37 (standard support until 1 December 2027), the newest EKS offered at creation; the rule is to take the newest at each creation (`scripts/eks_latest_version.py` checks it in the prod dry run); cluster upgrade policy STANDARD (no extended support); AL2023 arm64 EKS-optimized AMI. The Cluster Autoscaler runs one minor behind in `dev` (see D30 and the known limits) | Owner decision 2026-10-08: avoid being deprecated early; extended support costs extra. EKS auto-upgrades the control plane at the end of standard support, nodes do not |
| D2 | Pod permissions with EKS Pod Identity (agent add-on + pod identity associations), not IRSA | No OIDC provider to create, fixed trust principal, associations are CloudFormation resources |
| D3 | Node group `t4g.medium` on-demand, desired 2, min 2, max 4, gp3 20 GB encrypted with the data key, IMDSv2 required with hop limit 1, no key pair; nodes in the two private subnets | Two nodes = one per Availability Zone; the maximum is a cost cap decided by the owner |
| D4 | Authentication mode `API` (access entries only), `bootstrapClusterCreatorAdminPermissions=false`; entries: CodeBuild role (cluster admin policy, created by the `deploy` stack), bastion role (`AmazonEKSViewPolicy` plus the group `calc-operator-readonly` for nodes, created by the `bastion` stack, read-only); the node role through its standard node entry | No `aws-auth` ConfigMap, no implicit creator admin |
| D5 | Private endpoint only; the endpoint is reached by CodeBuild and the bastion through the cluster security group (443) | Constitution section 3 |
| D6 | One container image (repository `ecr-useast2-oecalc-eks-<env>`) with one command per workload (`add`, `sub`, `mul`, `div`, `history`, `ingest`, `migrate`), built from this repository, immutable tags; **deployments reference the image by digest, not by tag**; the CodeBuild role can only pull | Fewer repositories and scans than six; permissions are separated by pod identity, not by image; a tag cannot be repointed (design review threat 1) |
| D7 | Ingress with the AWS Load Balancer Controller (Helm, 1 replica in dev, 2 in prod): internet-facing ALB, IP targets, HTTPS (policy `ELBSecurityPolicy-TLS13-1-2-2021-06`: TLS 1.3 with TLS 1.2 fallback), HTTP redirect, two Ingress objects in one group (`calculator-preflight`, order 10, `OPTIONS` only and no token; `calculator-api`, order 20, `jwt-validation`: JWKS of the Cognito pool, issuer, `aud`, `token_use`), listener default fixed response 404, drop invalid headers, access logs to S3, tags from the controller's default tags | Native controller feature; verified in the controller documentation |
| D8 | Network policies enabled in the VPC CNI add-on: default deny in the application namespace, then one policy per workload (see D25 and D26) | Pod-level segmentation inside the node security group |
| D9 | Database access through RDS Proxy (TLS required, application secret registered, no IAM database authentication); the migration job and the rotation function connect to the instance directly | Constitution section 5 |
| D10 | Autoscaling: metrics-server (EKS add-on), HPA on CPU at 50 % of the request (minimum `replicaCount`: 1 in dev, 2 in prod; maximum 4) for `add`, `sub`, `mul`, `div`, `history`, scale-down stabilization 300 s; Cluster Autoscaler (Helm, Pod Identity) discovering the node group by tag | Requirement 17 |
| D11 | KMS: two keys with aliases `kms-useast2-oecalc-eks-<env>` (data) and `kms-useast2-oecalc-eks-secrets-<env>` (cluster secrets); IAM grants use `kms:ResourceAliases` with `kms:ViaService` (the callers are AWS services that send the key ARN) | Constitution section 3 and the KMS reference |
| D12 | CodeBuild project (ARM small, in the private subnets, one build at a time) gets its source from S3 (the workflow uploads the chart and scripts to the SAM artifact bucket); no GitHub connection. The workflow passes the SHA-256 of the source archive and the buildspec refuses to run if the downloaded archive differs; it runs only files from the verified copy. The buildspec is fixed in the project definition and is not read from the source. **Limit:** a principal that holds `codebuild:StartBuild` can still send a buildspec override at start time; the real controls are who holds `StartBuild` (only the deployment role) and the CloudTrail record, not the project definition (see the known limits) | No third-party OAuth app to create or trust; integrity of what is installed (design review threat 11) |
| D13 | The NAT gateway and the private default route are created and deleted by a scheduler Lambda (22:00 to 08:00 Panama time, `dev` only), as in `ecs`; its Elastic IP stays allocated. `prod` has one NAT gateway per Availability Zone, always on, created by CloudFormation | Requirement 16 |
| D14 | Names: `<acronym>-useast2-oecalc-eks[-<descriptor>]-<env>` (descriptor `eks` keeps names distinct from the `ecs` stacks in the same account); SSM registry path `/oecalc/<env>/api-backends/eks/` | Both versions can coexist |
| D15 | Scoped pod identities: the load balancer controller uses the AWS reference policy with tag conditions, the Cluster Autoscaler is limited to the Auto Scaling group of this node group (tag condition), each workload role reads only its own secret ARNs (the migrate role reads the master and the application secret, to create the application user), and only the CodeBuild role can create pods in `kube-system`; the history pod also reads the cursor key secret (no other workload does) | Threats 4 and 8 |
| D16 | The namespace `calculator` enforces Pod Security `restricted` and has a `ResourceQuota` and a `LimitRange`; pods cannot request more than a node can give | Threat 17 (autoscaler abuse) |
| D17 | TLS to the database and the proxy is verified (`verify-full`) with one trust file in the image (see D24), and the RDS bundle in the rotation function | Threat 7; closes gap G10 |
| D18 | Rotation function: never logs secret material, verifies the new credential before staging it as current, and may be invoked only by Secrets Manager (resource-scoped permission) | Threats 7 and 18 |
| D19 | `ssm:PutParameter` on `/oecalc/<env>/api-backends/eks/*` is allowed only to the CodeBuild role | Threat 9 |
| D20 | The NAT scheduler function's EC2 permissions are scoped to this NAT gateway and this route table by tag and ARN; after each run it verifies the default route and retries; a failed run raises an alarm | Threat 3 |
| D21 | The ingestion worker accepts only object keys that match `incoming/<name>.csv` (no path traversal, no extra prefixes) and inserts with `ON CONFLICT DO NOTHING`, so an existing row is never replaced | Threats 14 and 20 |
| D22 | An EventBridge rule sends AWS Health events for EKS to the alerts topic (with a dead-letter queue), and a weekly EventBridge Scheduler reminder publishes to the same topic with the cluster name, version and the date support ends | Owner request 2026-10-08; requirement 13.4 and 13.5 |
| D23 | Address plan (no overlap, so the VPCs can be peered or joined later): this project `dev` 10.30.0.0/16 and `prod` 10.31.0.0/16; `ecs` `dev` 10.20.0.0/16 and `prod` 10.21.0.0/16; `sls` has no VPC; the Kubernetes service range is 172.20.0.0/16 (inside the cluster, not routable from the VPC); the default VPC of each account is 172.31.0.0/16. Free for future projects: 10.40.0.0/16 and up | Owner question 2026-10-08; checked by a template test |

D1 to D23 are explained further in ADRs [0007](adr/0007-base-image-and-autoscaler-pinning.md), [0013](adr/0013-private-endpoint-codebuild-and-read-only-bastion.md) to [0018](adr/0018-token-validation-at-the-load-balancer.md); the index with the mapping is [adr/README.md](adr/README.md).

Decisions taken during implementation and the closing quality gate (2026-10-13). Each has an ADR.

| ID | Decision | ADR |
|---|---|---|
| D24 | RDS Proxy presents an ACM certificate, so the image trusts the RDS global bundle plus the four Amazon root CAs pinned by SHA-256 in one file; direct instance connections (migration, rotation) keep using the RDS bundle | [0001](adr/0001-rds-proxy-certificate-trust.md) |
| D25 | One network policy per workload (add, sub, mul, div, history, ingest, migrate) under a default deny, each listing only the egress it uses (DNS, database port to the database subnets, HTTPS to the AWS APIs, the Pod Identity agent) | [0002](adr/0002-per-workload-network-policies.md) |
| D26 | The API pods accept traffic only from the two public-subnet CIDRs where the load balancer nodes live (`network.albCidrs`), not from the whole VPC range, so no other pod can reach them and skip the JWT check | [0003](adr/0003-alb-only-ingress-cidrs.md) |
| D27 | Error alerts are limited to one per error type per interval (300 s by default) per process, with the number of suppressed alerts in the next one. This replaces "one email per error" of requirement 5.1 | [0004](adr/0004-alert-throttling.md) |
| D28 | The ingestion worker saves valid rows in batches (500 by default, one transaction per batch) | [0005](adr/0005-ingestion-batching.md) |
| D29 | Bounded waits everywhere: database connect and pool timeouts of 3 s, at most 20 requests waiting for a connection, AWS client timeouts (connect 2 s, read 3 s, 30 s for the queue long poll), and the alert publish runs in a worker thread so `/health` is never blocked | [0006](adr/0006-bounded-waits-and-timeouts.md) |
| D30 | Base image `python:3.14-slim-trixie` pinned by index digest and refreshed together with the tag; the Cluster Autoscaler may run one minor behind the cluster in `dev` only (`AllowAutoscalerMinorSkew`); prod stops the deployment instead of skipping it | [0007](adr/0007-base-image-and-autoscaler-pinning.md) |
| D31 | Production path: `Deploy PROD` runs on push to `main` (or by hand from `main`), first a plan job (dry run: change sets with the removal and replacement guard, cluster version check, nothing executed), then the apply job | [0008](adr/0008-production-path-on-main-with-dry-run.md) |
| D32 | Dependabot version updates once a week (pip, docker, GitHub Actions), grouped, at most 5 open pull requests per ecosystem; security updates stay immediate | [0009](adr/0009-dependabot-weekly.md) |
| D33 | Security groups for pods are not used: they need ENI trunking, which the `t4g` family does not support. Pod isolation relies on the network policies (D25, D26) and on credentials; gap G1 stays mitigated, not closed | [0010](adr/0010-no-security-groups-for-pods.md) |
| D34 | Accepted deviation of the naming table: alarms are `alm-` (table: `alrm`) and schedules `sch-` (table: `evs`); renaming forces replacement. A recreated environment should use `alrm-` and `evs-` from the start | [0011](adr/0011-alarm-and-schedule-name-deviation.md) |
| D35 | Library loggers (`botocore`, `boto3`, `urllib3`, `s3transfer`, `psycopg`) never log below WARNING whatever `LOG_LEVEL` says, and a redaction filter scrubs credential-looking values | [0012](adr/0012-log-level-cap-and-redaction.md) |

## Resource inventory

`<env>` is `dev` or `prod`; `<acct>` is built from a variable and never written in files. Stack = CloudFormation/SAM template that owns the resource (`stk-useast2-oecalc-eks-<unit>-<env>`); **Helm** = created by a chart; **controller** = created by the AWS Load Balancer Controller; **script** = created or changed by a deployment script. **Both** = `dev` and `prod`. A unit with `onlyIf` in `templates/stacks.json` is skipped when its parameter is not `true`: `scheduler` (`NatSchedulerEnabled`, dev only) and `bastion` (`EnableBastion`, dev only). Names were checked against the templates on 2026-10-13.

### Security (`security.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 1 | Data key and alias | `AWS::KMS::Key`, `Alias` | `alias/kms-useast2-oecalc-eks-<env>` | RDS, S3, SQS, logs, SNS, ECR, secrets, node volumes; window 7 days dev, 30 prod | Both |
| 2 | Cluster secrets key and alias | `AWS::KMS::Key`, `Alias` | `alias/kms-useast2-oecalc-eks-secrets-<env>` | Envelope encryption of Kubernetes secrets | Both |
| 3 | Alerts topic, policy and email subscription | `AWS::SNS::Topic`, `TopicPolicy`, `Subscription` | `sns-useast2-oecalc-eks-alerts-<env>` | Errors and alarms; TLS-only policy; the subscription must be confirmed by hand once | Both |

### Network (`network.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 4 | VPC | `AWS::EC2::VPC` | `vpc-useast2-oecalc-eks-<env>` | Private network (10.30.0.0/16 dev, 10.31.0.0/16 prod) | Both |
| 5 | Public subnets (2 AZ) | `AWS::EC2::Subnet` | `snet-useast2-oecalc-eks-public-a/b-<env>` | ALB, NAT; tag `kubernetes.io/role/elb` | Both |
| 6 | Private subnets (2 AZ) | `AWS::EC2::Subnet` | `snet-useast2-oecalc-eks-private-a/b-<env>` | Nodes, database, proxy, CodeBuild, bastion, rotation function; tag `kubernetes.io/role/internal-elb` | Both |
| 7 | Internet gateway, route tables and associations, public default route | `AWS::EC2::InternetGateway`, `VPCGatewayAttachment`, `RouteTable`, `Route`, `SubnetRouteTableAssociation` | `igw-useast2-oecalc-eks-<env>`, `rtb-useast2-oecalc-eks-public-<env>`, `rtb-useast2-oecalc-eks-private-<env>`, `rtb-useast2-oecalc-eks-private-b-<env>` | Routing; the second private table exists in prod only (one table per zone) | Both (`private-b`: prod) |
| 8 | S3 gateway endpoint | `AWS::EC2::VPCEndpoint` | `vpce-useast2-oecalc-eks-s3-<env>` | Free S3 path | Both |
| 9 | Elastic IPs | `AWS::EC2::EIP` | `eip-useast2-oecalc-eks-nat-<env>`, `eip-useast2-oecalc-eks-nat-b-<env>` | NAT addresses; zone b only in prod | Both (b: prod) |
| 10 | NAT gateways and private default routes | `AWS::EC2::NatGateway`, `Route` | prod: `nat-useast2-oecalc-eks-a/b-prod`; dev: `nat-useast2-oecalc-eks-dev` (created by the scheduler function, tag `Name`) | Egress of the private subnets. Prod: always on, one per AZ, owned by the network stack. Dev: created at 08:00 and deleted at 22:00 by the scheduler, never owned by CloudFormation | Both |
| 11 | Security groups (prefix `sgp`) | `AWS::EC2::SecurityGroup` | `sgp-useast2-oecalc-eks-<role>-<env>` for `alb`, `node`, `db`, `proxy`, `codebuild`, `bastion`, `rotation` | See the security design review, section 3.8 | Both |
| 12 | Security group rules between them | `AWS::EC2::SecurityGroupIngress`, `SecurityGroupEgress` (14) | (none) | ALB to pods, node DNS, node to proxy and database, proxy to database, rotation to database | Both |
| 13 | VPC flow log, role, policy and log group | `AWS::EC2::FlowLog`, `IAM::Role`, `IAM::RolePolicy`, `Logs::LogGroup` | `flog-useast2-oecalc-eks-<env>`, `iamr-useast2-oecalc-eks-flowlog-<env>`, `/oecalc/<env>/eks/vpc-flow` | VPC-01 | prod |

Not a resource: the VPC default security group is emptied of all rules (and tagged) by `scripts/cfn_deploy.py` (`lock_default_security_group`) after the network stack is deployed.

### Registry (`registry.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 14 | ECR repository + lifecycle + pull-only policy | `AWS::ECR::Repository` | `ecr-useast2-oecalc-eks-<env>` | One image, scan on push, immutable tags, keep the last 10, KMS by alias, `EmptyOnDelete` per environment | Both |

### Scheduler (`scheduler.yaml`, SAM)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 15 | NAT scheduler function, its role and policy, log group | `AWS::Serverless::Function`, `IAM::Role`, `IAM::RolePolicy`, `Logs::LogGroup` | `fnc-useast2-oecalc-eks-nat-<env>`, `iamr-useast2-oecalc-eks-nat-fnc-<env>`, `/aws/lambda/fnc-useast2-oecalc-eks-nat-<env>` | Creates and deletes the NAT gateway and its default route | dev |
| 16 | Schedules (down 22:00, up 08:00 Panama), scheduler role and policy | `AWS::Scheduler::Schedule`, `IAM::Role`, `IAM::RolePolicy` | `sch-useast2-oecalc-eks-nat-down/up-<env>`, `iamr-useast2-oecalc-eks-nat-sch-<env>` | Invoke the function | dev |
| 17 | Keep-up parameter | `AWS::SSM::Parameter` | `/oecalc/<env>/eks/nat-keep-up` | `true` skips the nightly shutdown (workflow `Keep NAT`); default `false` | dev |
| 18 | Function error alarm | `AWS::CloudWatch::Alarm` | `alm-useast2-oecalc-eks-nat-errors-<env>` | A failed run raises an email | dev |

### Database (`database.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 19 | DB subnet group, parameter group | `AWS::RDS::DBSubnetGroup`, `DBParameterGroup` | `dbsg-useast2-oecalc-eks-<env>`, `dbpg-useast2-oecalc-eks-<env>` | `rds.force_ssl=1` | Both |
| 20 | RDS PostgreSQL 17 instance (two conditional definitions with literal deletion policies) | `AWS::RDS::DBInstance` | `rds-useast2-oecalc-eks-<env>` | `db.t4g.micro`, gp3 20 GB, CMK, `calcadmin` master with a secret managed by RDS; Single-AZ dev, Multi-AZ prod; backups 1 day dev, 15 days prod; deletion protection and final snapshot in prod; a `Rules` block refuses a prod parameter file that turns these off | Both |
| 21 | Engine log groups | `AWS::Logs::LogGroup` | `/aws/rds/instance/rds-useast2-oecalc-eks-<env>/postgresql`, `.../upgrade` | Log exports (on in prod) | Both |
| 22 | Application secret and policy | `AWS::SecretsManager::Secret`, `ResourcePolicy` | `sm-useast2-oecalc-eks-app-<env>` | Limited user `calc_app`; no host in the secret; TLS-only policy | Both |
| 23 | History cursor key secret and policy | `AWS::SecretsManager::Secret`, `ResourcePolicy` | `sm-useast2-oecalc-eks-cursor-<env>` | JSON key `cursor_key`, data key, no rotation (a new value only invalidates cursors in flight: 400); read only by the history pod | Both |
| 24 | Master secret policy | `AWS::SecretsManager::ResourcePolicy` | (secret named by AWS) | Master secret is created and rotated by RDS; administrator, migration job and rotation function only | Both |
| 25 | RDS Proxy, target group, role and policy, log group | `AWS::RDS::DBProxy`, `DBProxyTargetGroup`, `IAM::Role`, `IAM::RolePolicy`, `Logs::LogGroup` | `rdsp-useast2-oecalc-eks-<env>`, `iamr-useast2-oecalc-eks-proxy-<env>`, `/aws/rds/proxy/rdsp-useast2-oecalc-eks-<env>` | TLS required, pooled connections for the application user, no IAM database authentication | Both |

### Rotation (`rotation.yaml`, SAM)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 26 | Rotation function, role and policy, log group, invoke permission | `AWS::Serverless::Function`, `IAM::Role`, `IAM::RolePolicy`, `Logs::LogGroup`, `Lambda::Permission` | `fnc-useast2-oecalc-eks-db-rotation-<env>`, `iamr-useast2-oecalc-eks-db-rotation-<env>` | In the private subnets; alternating-user strategy; invoked only by Secrets Manager | Both |
| 27 | Rotation schedules | `AWS::SecretsManager::RotationSchedule` (2) | (none) | Application secret: every 3 days in a window starting 14:00 UTC for 8 h (09:00 to 17:00 Panama); master secret: managed by RDS on the days in between | Both |
| 28 | Function error alarm | `AWS::CloudWatch::Alarm` | `alm-useast2-oecalc-eks-db-rotation-errors-<env>` | Failed rotation raises an email | Both |

### Cluster (`cluster.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 29 | EKS cluster | `AWS::EKS::Cluster` | `eksc-useast2-oecalc-<env>` | Kubernetes 1.37, private endpoint only, secrets encryption, API authentication mode, standard support, five control plane log types (`api`, `audit`, `authenticator`, `controllerManager`, `scheduler`), deletion protection in prod | Both |
| 30 | Cluster role (and secrets-key policy), node role | `AWS::IAM::Role`, `IAM::RolePolicy` | `iamr-useast2-oecalc-eks-cluster-<env>`, `iamr-useast2-oecalc-eks-node-<env>` | Managed policies for cluster, worker node, CNI, ECR read-only | Both |
| 31 | Cluster log group | `AWS::Logs::LogGroup` | `/aws/eks/eksc-useast2-oecalc-<env>/cluster` | Control plane logs, CMK, 14 days dev, 90 prod | Both |
| 32 | Launch template and managed node group | `AWS::EC2::LaunchTemplate`, `AWS::EKS::Nodegroup` | `lt-useast2-oecalc-eks-node-<env>`, `eksng-useast2-oecalc-<env>` | `t4g.medium` arm64, 2 to 4, IMDSv2 hop 1, tags on instance, volume and primary network interface, autoscaler tags | Both |
| 33 | EKS add-ons (pinned versions) | `AWS::EKS::Addon` (5) | `vpc-cni` (network policy on), `kube-proxy`, `eks-pod-identity-agent`, `coredns`, `metrics-server` | Cluster basics, Pod Identity credentials, metrics for the HPA | Both |
| 34 | Pod identity roles, policies and associations | `AWS::IAM::Role`, `IAM::RolePolicy`, `EKS::PodIdentityAssociation` | `iamr-useast2-oecalc-eks-<lbc/autoscaler/calc/history/migrate>-<env>` | Least privilege per service account; `calc` serves add, sub, mul and div | Both |

### Deploy (`deploy.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 35 | CodeBuild project, role, log group | `AWS::CodeBuild::Project`, `IAM::Role`, `Logs::LogGroup` | `cbp-useast2-oecalc-eks-deploy-<env>`, `iamr-useast2-oecalc-eks-deploy-<env>`, `/aws/codebuild/cbp-useast2-oecalc-eks-deploy-<env>` | ARM small in the private subnets, 30 minute timeout; runs helm and kubectl, the migration trigger and the SSM publication | Both |
| 36 | Administrator access entry and cluster rule | `AWS::EKS::AccessEntry`, `EC2::SecurityGroupIngress` | (CodeBuild role) | Cluster admin for deployment only; 443 from the CodeBuild security group to the cluster security group | Both |

### Bastion (`bastion.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 37 | Bastion instance, role, instance profile | `AWS::EC2::Instance`, `IAM::Role`, `IAM::InstanceProfile` | `ec2-useast2-oecalc-eks-bastion-<env>`, `iamr-useast2-oecalc-eks-bastion-<env>` | `t4g.medium` (4 GiB, because `k9s`, `helm` and load tools run at once; a smaller size ran out of memory), gp3 20 GiB encrypted, standard CPU credits, no public IP; Session Manager only; `kubectl`, `helm`, AWS CLI, `k9s`, `stern`, `jq` installed once at first boot with pinned checksums | dev (parameter) |
| 38 | Session preferences document and session log group | `AWS::SSM::Document`, `Logs::LogGroup` | `ssmd-useast2-oecalc-eks-bastion-<env>`, `/aws/ssm/session/useast2-oecalc-eks-bastion-<env>` | Sessions logged to an encrypted group, 20 minute idle timeout | dev |
| 39 | Read-only access entry and cluster rule | `AWS::EKS::AccessEntry`, `EC2::SecurityGroupIngress` | (bastion role) | `AmazonEKSViewPolicy` plus group `calc-operator-readonly`; edit rights in namespace `calculator` only with the parameter `BastionWriteAccess` | dev |

### Storage (`storage.yaml`)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 40 | Ingestion queue, dead-letter queue and policies | `AWS::SQS::Queue`, `QueuePolicy` | `sqs-useast2-oecalc-eks-ingest-<env>`, `sqs-useast2-oecalc-eks-ingest-dlq-<env>` | S3 event, long poll, visibility 300 s, 3 receives then DLQ, KMS, TLS-only | Both |
| 41 | Ingestion bucket and policy | `AWS::S3::Bucket`, `BucketPolicy` | `bckt-useast2-oecalc-eks-ingest-<acct>-<env>` | `incoming/`, `processed/`, `rejected/`, `reports/`; CMK, versioning, Block Public Access, TLS-only, lifecycle per prefix | Both |
| 42 | Load balancer log bucket and policy | `AWS::S3::Bucket`, `BucketPolicy` | `bckt-useast2-oecalc-eks-alb-logs-<acct>-<env>` | SSE-S3 (log delivery limitation), lifecycle 30 days dev, 90 prod | Both |
| 43 | Ingestion pod identity role, policy and association | `AWS::IAM::Role`, `IAM::RolePolicy`, `EKS::PodIdentityAssociation` | `iamr-useast2-oecalc-eks-ingest-<env>` | Queue, bucket prefixes and application secret only (lives here because the bucket and queue ARNs are known here) | Both |

### Alarms (`alarms.yaml`, deployed last, after the Helm release)

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 44 | 17 alarms | `AWS::CloudWatch::Alarm` | `alm-useast2-oecalc-eks-<what>-<env>`, `<what>` = `nodes-in-service`, `healthy-hosts-add/sub/mul/div/history`, `alb-5xx`, `target-5xx`, `rds-free-storage`, `rds-connections`, `rds-cpu`, `rds-memory`, `proxy-auth-errors`, `proxy-db-errors`, `ingest-dlq`, `ingest-queue-age`, `build-failures` | Nodes in service below the minimum, no healthy target per workload, 5XX, database and proxy health, DLQ messages, oldest message age, failed deployment builds. Dimensions (node group, load balancer, target groups) are found by tag after the release by `scripts/discover_targets.py` | Both |
| 45 | Health events rule, its dead-letter queue and policy | `AWS::Events::Rule`, `SQS::Queue`, `QueuePolicy` | `evr-useast2-oecalc-eks-health-<env>`, `sqs-useast2-oecalc-eks-health-events-dlq-<env>` | AWS Health events of EKS to the alerts topic | Both |
| 46 | Weekly reminder schedule, role and policy | `AWS::Scheduler::Schedule`, `IAM::Role`, `IAM::RolePolicy` | `sch-useast2-oecalc-eks-reminder-<env>`, `iamr-useast2-oecalc-eks-reminder-sch-<env>` | `rate(7 days)`: the first reminder arrives 7 days after creation; cluster name, version and end of support | Both |

The alerts topic receives 19 alarms in total: 17 here, one in `rotation` and one in `scheduler` (dev).

### Created by Helm, controllers and scripts

| # | Resource | Type | Name | Purpose | Env |
|---|---|---|---|---|---|
| 47 | Helm release: AWS Load Balancer Controller | Helm | `aws-load-balancer-controller` (namespace `kube-system`), chart 3.6.0, pinned by SHA-256 | Creates the ALB from the Ingress | Both |
| 48 | Helm release: Cluster Autoscaler | Helm | `cluster-autoscaler` (namespace `kube-system`), chart 9.59.0, image v1.36.1 | Scales the node group | Both (see the known limits) |
| 49 | Read-only cluster role and binding for the bastion group | `kubectl apply` of `chart-addons/operator-rbac.yaml` | `calc-operator-nodes-readonly` | Nodes and node metrics for the group `calc-operator-readonly` | Both |
| 50 | Helm release: calculator | Helm | `calculator` (namespace `calculator`) | Deployments `add`, `sub`, `mul`, `div`, `history`, `ingest`; services; HPAs; PodDisruptionBudgets (prod, more than one replica); network policies; service accounts `calc`, `history`, `migrate`, `ingest`; two Ingress objects | Both |
| 51 | Namespace `calculator` with Pod Security `restricted`, `ResourceQuota`, `LimitRange` | Helm | `calculator` | Limits and policy | Both |
| 52 | Migration job | Kubernetes `Job` rendered by the chart and applied before the release | `migrate-r<run id>` | Schema and application user, once per deployment | Both |
| 53 | Application load balancer, listeners, rules, target groups (two per service, one per Ingress), controller security groups | created by the controller | `k8s-...` names set by the controller, tags from the controller default tags | Public entrance; rules use the existing ALB security group of the network stack | Both |
| 54 | SSM registration parameters | `aws ssm put-parameter` by `scripts/helm_deploy.sh` (not CloudFormation) | `/oecalc/<env>/api-backends/eks/dns-name`, `.../hosted-zone-id` | Read by `shared-resources`; removed by the destroy workflow | Both |
| 55 | WAF association | Ingress annotation | (shared regional ACL by ARN, variable `WAF_ACL_ARN`) | Regional ACL; the chart refuses prod without it | prod |
| 56 | Auto Scaling group metric `GroupInServiceInstances` | `enable-metrics-collection` by `scripts/discover_targets.py` | (the node group's group) | Feeds the nodes-in-service alarm; an idempotent write by the pipeline | Both |

Environment differences visible in the table: `dev` has the bastion, the NAT scheduler and one NAT gateway, Single-AZ database, 1 day of backups and a controller replica; `prod` has two NAT gateways, flow logs, Multi-AZ database with deletion protection, 15 days of backups, engine log exports, two replicas minimum, a WAF ACL and a deletion-protected cluster. Everything else is the same template.

## Known limits and accepted items

Recorded so that nobody over-trusts a control (closing quality gate, 2026-10-13).

- **Node egress.** Nodes carry both the node security group and the EKS cluster security group; security groups only add permissions, so the narrow egress of the node group does not restrict anything. Egress is limited by the network policies only, and the proxy and database rules admit any member of the node security group (gap G1).
- **Pods are not isolated by security group** (D33).
- **Build override.** `codebuild:StartBuild` allows a buildspec override (D12). The control is who holds it.
- **No pod log shipping.** Pod logs are read with `kubectl logs` from the bastion; they are not in CloudWatch. No Container Insights and no pod alarms exist: the alarms cover nodes in service, healthy targets per workload, load balancer 5XX, database, proxy, queues and builds. Backlog.
- **No persistent volumes.** The cluster uses none; the default `gp2` storage class is never used.
- **Autoscaler version.** Cluster Autoscaler v1.36.1 runs on a 1.37 cluster in `dev` with the owner-accepted parameter `AllowAutoscalerMinorSkew=true`. `prod` keeps it `false`, so the add-ons step fails (it no longer skips silently) until the pinned chart and image match the cluster minor.
- **Open items before the first production deployment** (none is implemented): deletion protection on the load balancer (it must be paired with a "lift protection" step in the uninstall), S3 server access logs for the buckets, `ADDITIONAL_ENI_TAGS` for the network interfaces made by the VPC CNI (unverified), an on-failure destination for the rotation function, the service-linked role `AutoScalingServiceLinkedRoleExists=false`, the `WAF_ACL_ARN` variable, sizing of the alarm thresholds and the quota.
- **Worker probes.** The ingestion worker has no liveness probe; a hung worker is noticed only by the queue-age alarm.
- **Add-on images.** The controller and autoscaler images are pulled by tag from public registries through the NAT; the chart archives are pinned by SHA-256, the images are not pinned by digest.
- **Placeholder rule.** The database and proxy security groups carry a documented egress rule to 127.0.0.1/32 so that EC2 does not add its default allow-all egress; it allows no traffic.
- **Control plane logs.** Five log types are enabled (the design listed three); `controllerManager` and `scheduler` add a little ingestion cost.
- **Bastion.** `t4g.medium` instead of the smallest instance (memory), public Amazon Linux image, not a golden image.
- **S3.** No AWS Backup plan and no Macie access for the ingestion bucket; data is non-personal and versioned.
- **Accepted gaps of the design review** G1 to G19 stand as recorded there (KMS key policies trust the account root, cluster security group without tags, no image signing, WAF only in prod).
