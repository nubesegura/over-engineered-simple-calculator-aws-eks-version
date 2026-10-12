# Architecture

> Status: design for review (stage 5 of spec `001-create-baseline`); nothing is deployed. Updated at closing against the real templates.
> Diagram: [architecture.drawio](architecture.drawio) (export `architecture.png`: File > Export as > PNG, 2x).

## Overview

```
Browser --> Route 53 (weights, shared-resources) --> ALB (443, JWT validation) --> pods (EKS managed node group, private subnets)
                                                         |                           |--> RDS Proxy --> RDS PostgreSQL
                                                         +--> Cognito JWKS (443)     +--> NAT (08:00-22:00) --> ECR, Secrets Manager, SNS, Logs, STS
S3 incoming/*.csv --> SQS (+DLQ) --> ingestion worker (pod) --> RDS Proxy
GitHub Actions --OIDC--> CodeBuild (in the VPC) --> private EKS API (helm, kubectl)        Owner --Session Manager--> bastion (read-only kubectl)
```

## Design decisions (stage 5)

| ID | Decision | Reason |
|---|---|---|
| D1 | Kubernetes 1.37 (standard support until 1 December 2027), the newest EKS offers; the rule is to take the newest at each creation; cluster upgrade policy STANDARD (no extended support); AL2023 arm64 EKS-optimized AMI. The Cluster Autoscaler and controller chart versions must support the minor (checked when the templates are written) | Owner decision 2026-10-08: avoid being deprecated early; extended support costs extra. EKS auto-upgrades the control plane at the end of standard support, nodes do not |
| D2 | Pod permissions with EKS Pod Identity (agent add-on + pod identity associations), not IRSA | No OIDC provider to create, fixed trust principal, associations are CloudFormation resources |
| D3 | Node group `t4g.medium` on-demand, desired 2, min 2, max 4, gp3 20 GB encrypted with the data key, IMDSv2 required with hop limit 1, no key pair; nodes in the two private subnets | Two nodes = one per Availability Zone; the maximum is a cost cap decided by the owner |
| D4 | Authentication mode `API` (access entries only), `bootstrapClusterCreatorAdminPermissions=false`; entries: CodeBuild role (cluster admin policy), bastion role (`AmazonEKSViewPolicy`, read-only); the node role through its standard node entry | No `aws-auth` ConfigMap, no implicit creator admin |
| D5 | Private endpoint only; the endpoint is reached by CodeBuild and the bastion through the cluster security group (443) | Constitution section 3 |
| D6 | One container image (repository `ecr-useast2-oecalc-eks-dev`) with one command per workload (`add`, `sub`, `mul`, `div`, `history`, `ingest`, `migrate`), built from this repository, immutable tags; **deployments reference the image by digest, not by tag**; the CodeBuild role can only pull | Fewer repositories and scans than six; permissions are separated by pod identity, not by image; a tag cannot be repointed (Security Agent threat 1) |
| D7 | Ingress with the AWS Load Balancer Controller (Helm, 1 replica in dev): internet-facing ALB, IP targets, HTTPS (TLS 1.3 policy), redirect, `jwt-validation` annotation (JWKS of the Cognito pool, issuer, `aud`, `token_use`), drop invalid headers, access logs to S3, tags from the controller's default tags | Native controller feature; verified in the controller documentation |
| D8 | Network policies enabled in the VPC CNI add-on: default deny in the application namespace, allow ALB to pods, pods to RDS Proxy, DNS, and egress 443 | Pod-level segmentation inside the node security group |
| D9 | Database access through RDS Proxy (TLS required, application secret registered, no IAM database authentication); migration job and rotation function connect to the instance directly | Constitution section 5 |
| D10 | Autoscaling: metrics-server (EKS add-on), HPA on CPU at 50 % (1 to 4 replicas) for `add`, `sub`, `mul`, `div`, `history`; Cluster Autoscaler (Helm, Pod Identity) on the node group tags | Requirement 17 |
| D11 | KMS: two keys with aliases `kms-useast2-oecalc-eks-dev` (data) and `kms-useast2-oecalc-eks-secrets-dev` (cluster secrets); IAM grants use `kms:ResourceAliases` with `kms:ViaService` (the callers are AWS services that send the key ARN) | Constitution section 3 and the KMS reference |
| D12 | CodeBuild project (ARM small, in the private subnets) gets its source from S3 (the workflow uploads the chart and scripts to the SAM artifact bucket); no GitHub connection. The workflow passes the SHA-256 of the source archive and CodeBuild refuses to run if it differs; the buildspec is fixed in the template and the pipeline role cannot override it | No third-party OAuth app to create or trust; integrity of what is installed (Security Agent threat 11 and design-review finding on software integrity) |
| D13 | The NAT gateway and the private default route are created and deleted by a scheduler Lambda (22:00 to 08:00 Panama time, `dev` only), as in `ecs`; its Elastic IP stays allocated | Requirement 16 |
| D14 | Names: `<acronym>-useast2-oecalc-eks[-<descriptor>]-<env>` (descriptor `eks` keeps names distinct from the `ecs` stacks in the same account); SSM registry path `/oecalc/<env>/api-backends/eks/` | Both versions can coexist |
| D15 | Scoped pod identities: the load balancer controller uses the AWS reference policy with tag conditions, the Cluster Autoscaler is limited to the Auto Scaling group of this node group (tag condition), each workload role reads only its own secret ARNs (the migrate role reads the master and the application secret, to create the application user), and only the CodeBuild role can create pods in `kube-system`; the history pod also reads the cursor key secret (no other workload does) | Threats 4 and 8 |
| D16 | The namespace `calculator` has a `ResourceQuota` and a `LimitRange`; pods cannot request more than a node can give | Threat 17 (autoscaler abuse) |
| D17 | TLS to the database and the proxy is verified (`verify-full` with the RDS global CA bundle packaged in the image and the rotation function) | Threat 7; closes gap G10 |
| D18 | Rotation function: never logs secret material, verifies the new credential before staging it as current, and may be invoked only by Secrets Manager (resource-scoped permission) | Threats 7 and 18 |
| D19 | `ssm:PutParameter` on `/oecalc/<env>/api-backends/eks/*` is allowed only to the CodeBuild role | Threat 9 |
| D20 | The NAT scheduler function's EC2 permissions are scoped to this NAT gateway and this route table by tag and ARN; after each run it verifies the default route and retries; a failed run raises an alarm | Threat 3 |
| D21 | The ingestion worker accepts only object keys that match `incoming/<name>.csv` (no path traversal, no extra prefixes) and inserts with `ON CONFLICT DO NOTHING`, so an existing row is never replaced | Threats 14 and 20 |
| D22 | An EventBridge rule sends AWS Health events for EKS to the alerts topic, and a weekly EventBridge Scheduler reminder publishes to the same topic with the cluster name, version and the date support ends | Owner request 2026-10-08; requirement 13.4 and 13.5 |
| D23 | Address plan (no overlap, so the VPCs can be peered or joined later): this project `dev` 10.30.0.0/16 and `prod` 10.31.0.0/16; `ecs` `dev` 10.20.0.0/16 and `prod` 10.21.0.0/16; `sls` has no VPC; the Kubernetes service range is 172.20.0.0/16 (inside the cluster, not routable from the VPC); the default VPC of each account is 172.31.0.0/16. Free for future projects: 10.40.0.0/16 and up | Owner question 2026-10-08; checked by a template test |

## Resource inventory

`<env>` is `dev` or `prod`; `<acct>` is built from a variable and never written in files. Stack = CloudFormation/SAM template that owns the resource (`stk-useast2-oecalc-eks-<stack>-<env>`); **Helm** = created by the chart or a controller. **Both** = `dev` and `prod`.

| # | Resource | Type | Planned name | Purpose | Env | Stack |
|---|---|---|---|---|---|---|
| 1 | VPC | `AWS::EC2::VPC` | `vpc-useast2-oecalc-eks-<env>` | Private network | Both | network |
| 2 | Public subnets (2 AZ) | `AWS::EC2::Subnet` | `snet-useast2-oecalc-eks-public-a/b-<env>` | ALB, NAT; tag `kubernetes.io/role/elb` | Both | network |
| 3 | Private subnets (2 AZ) | `AWS::EC2::Subnet` | `snet-useast2-oecalc-eks-private-a/b-<env>` | Nodes, database, proxy, CodeBuild, bastion; tag `kubernetes.io/role/internal-elb` | Both | network |
| 4 | Internet gateway, route tables | `AWS::EC2::InternetGateway`, `RouteTable` | `igw-...`, `rtb-useast2-oecalc-eks-public/private-<env>` | Routing; the private default route is created by the scheduler | Both | network |
| 5 | NAT gateway (created daily by the scheduler) and Elastic IP | EC2 API, `AWS::EC2::EIP` | `nat-useast2-oecalc-eks-<env>`, `eip-useast2-oecalc-eks-nat-<env>` | Egress of the private subnets | Both (prod: one per AZ, always on) | scheduler, network |
| 6 | S3 gateway endpoint | `AWS::EC2::VPCEndpoint` | `vpce-useast2-oecalc-eks-s3-<env>` | Free S3 path | Both | network |
| 7 | Default security group, locked | `AWS::EC2::SecurityGroupEgress/Ingress` removal | (default) | VPC-02 | Both | network |
| 8 | Security groups (prefix `sgp`): ALB, node, database, proxy, CodeBuild, bastion, rotation | `AWS::EC2::SecurityGroup` | `sgp-useast2-oecalc-eks-<role>-<env>` | See the security design review, section 3.8 | Both | network |
| 9 | VPC flow log | `AWS::EC2::FlowLog` + role + log group | `flog-useast2-oecalc-eks-<env>` | VPC-01 | prod | network |
| 10 | KMS keys and aliases (2) | `AWS::KMS::Key`, `Alias` | `kms-useast2-oecalc-eks-<env>`, `kms-useast2-oecalc-eks-secrets-<env>` | Data (RDS, S3, SQS, logs, SNS, ECR, secrets); cluster secrets envelope | Both | security |
| 11 | SNS topic + email subscription | `AWS::SNS::Topic`, `Subscription` | `sns-useast2-oecalc-eks-alerts-<env>` | Errors and alarms | Both | security |
| 12 | ECR repository + lifecycle | `AWS::ECR::Repository` | `ecr-useast2-oecalc-eks-<env>` | One image, scan on push, immutable tags, keep 10, `EmptyOnDelete` | Both | registry |
| 13 | DB subnet group, parameter group | `AWS::RDS::DBSubnetGroup`, `DBParameterGroup` | `dbsg-...`, `dbpg-useast2-oecalc-eks-<env>` | `rds.force_ssl=1` | Both | database |
| 14 | RDS PostgreSQL instance | `AWS::RDS::DBInstance` | `rds-useast2-oecalc-eks-<env>` | db.t4g.micro gp3 20 GB, CMK; Single-AZ dev, Multi-AZ prod; backups 1 day dev, 15 days prod | Both | database |
| 15 | RDS Proxy + target group + role | `AWS::RDS::DBProxy`, `DBProxyTargetGroup` | `rdsp-useast2-oecalc-eks-<env>`, `iamr-useast2-oecalc-eks-proxy-<env>` | Pooled TLS connections for the application user | Both | database |
| 16 | Master secret (managed by RDS) and rotation | managed by `DBInstance` + `AWS::SecretsManager::RotationSchedule` | (name set by AWS) | Administrator, migration, rotation function only | Both | database |
| 17 | Application secret + rotation schedule | `AWS::SecretsManager::Secret`, `RotationSchedule` | `sm-useast2-oecalc-eks-app-<env>` | Limited user, alternating-user rotation every 3 days | Both | rotation |
| 17b | History cursor key secret | `AWS::SecretsManager::Secret` | `sm-useast2-oecalc-eks-cursor-<env>` | Generated JSON key `cursor_key`, data key, TLS-only policy, no rotation (a new value only invalidates cursors in flight: 400); read only by the history pod | Both | database |
| 18 | Rotation function, role, log group | `AWS::Serverless::Function`, `AWS::Logs::LogGroup` | `fnc-useast2-oecalc-eks-db-rotation-<env>` | In the private subnets | Both | rotation |
| 19 | NAT scheduler function, role, 2 schedules | `AWS::Serverless::Function`, `AWS::Scheduler::Schedule` | `fnc-useast2-oecalc-eks-nat-<env>`, `sch-useast2-oecalc-eks-nat-down/up-<env>` | Creates and deletes the NAT and its default route | dev | scheduler |
| 20 | Ingestion bucket | `AWS::S3::Bucket` + policy | `bckt-useast2-oecalc-eks-ingest-<acct>-<env>` | `incoming/`, `processed/`, `rejected/`, `reports/`; CMK, versioning, BPA, TLS-only | Both | storage |
| 21 | ALB log bucket | `AWS::S3::Bucket` + policy | `bckt-useast2-oecalc-eks-alblogs-<acct>-<env>` | SSE-S3 (log delivery limitation), lifecycle | Both | storage |
| 22 | Ingestion queue and DLQ | `AWS::SQS::Queue` | `sqs-useast2-oecalc-eks-ingest-<env>`, `...-ingest-dlq-<env>` | S3 event, retries, DLQ (CMK) | Both | storage |
| 23 | EKS cluster | `AWS::EKS::Cluster` | `eksc-useast2-oecalc-<env>` | Private endpoint, secrets encryption, control plane logs, access entries | Both | cluster |
| 24 | Cluster role, node role | `AWS::IAM::Role` | `iamr-useast2-oecalc-eks-cluster-<env>`, `iamr-useast2-oecalc-eks-node-<env>` | Managed policies for cluster, worker node, CNI, ECR read-only | Both | cluster |
| 25 | Managed node group + launch template | `AWS::EKS::Nodegroup`, `AWS::EC2::LaunchTemplate` | `eksng-useast2-oecalc-<env>` | t4g.medium arm64, 2 to 4, IMDSv2 hop 1, autoscaler tags | Both | cluster |
| 26 | EKS add-ons | `AWS::EKS::Addon` | vpc-cni (network policy on), coredns, kube-proxy, eks-pod-identity-agent, metrics-server | Pinned versions | Both | cluster |
| 27 | Access entries | `AWS::EKS::AccessEntry` | (CodeBuild role, bastion role) | Admin for deployment, view for the owner | Both | cluster, bastion |
| 28 | Pod identity associations and roles | `AWS::EKS::PodIdentityAssociation`, `AWS::IAM::Role` | `iamr-useast2-oecalc-eks-<workload>-<env>` for `lbc`, `autoscaler`, `calc` (add, sub, mul, div), `history`, `ingest`, `migrate` | Least privilege per workload | Both | cluster |
| 29 | Cluster log group | `AWS::Logs::LogGroup` | `/aws/eks/eksc-useast2-oecalc-<env>/cluster` | Control plane logs, CMK, 14 days dev, 90 prod | Both | cluster |
| 30 | CodeBuild project, role, log group, SG | `AWS::CodeBuild::Project` | `cbp-useast2-oecalc-eks-deploy-<env>` | In the VPC; runs helm, kubectl, migration trigger, SSM publication | Both | deploy |
| 31 | Bastion instance, role, profile, SG, session log group, session preferences | `AWS::EC2::Instance`, `AWS::SSM::Document` | `ec2-useast2-oecalc-eks-bastion-<env>` | t4g.medium (4 GiB, for k9s, helm and load tools at once), gp3 20 GiB encrypted, standard CPU credits; Session Manager only, read-only kubectl, k9s, stern, jq | dev (parameter) | bastion |
| 32 | Helm release: AWS Load Balancer Controller | Helm | `aws-load-balancer-controller` (namespace `kube-system`) | Creates the ALB from the Ingress | Both | Helm |
| 33 | Helm release: Cluster Autoscaler | Helm | `cluster-autoscaler` (namespace `kube-system`) | Scales the node group | Both | Helm |
| 34 | Helm release: calculator | Helm | `calculator` (namespace `calculator`) | Deployments (add, sub, mul, div, history, ingest worker), services, HPAs, PDBs, network policies, service accounts, Ingress | Both | Helm |
| 35 | Application load balancer, listeners, rules, target groups, security groups | created by the controller | `k8s-...` names set by the controller, tags from the chart | Public entrance | Both | Helm (controller) |
| 36 | SSM parameters published | `AWS::SSM::Parameter` created by the deployment | `/oecalc/<env>/api-backends/eks/dns-name`, `.../hosted-zone-id` | Read by `shared-resources` | Both | deploy script |
| 37 | Alarms (about 20) | `AWS::CloudWatch::Alarm` | `alm-useast2-oecalc-eks-<what>-<env>` | Nodes not ready, pods, ALB 5XX and healthy hosts, RDS, DLQ, rotation, scheduler, build failures | Both | alarms |
| 38 | WAF association | Ingress annotation | (shared ACL by ARN) | Regional ACL | prod | Helm |
| 39 | EventBridge rule for AWS Health events of EKS + weekly reminder schedule + target permissions | `AWS::Events::Rule`, `AWS::Scheduler::Schedule` | `evr-useast2-oecalc-eks-health-<env>`, `sch-useast2-oecalc-eks-reminder-<env>` | Upgrade and still-running notices by email | Both | alarms |
| 40 | Namespace `calculator` with Pod Security `restricted`, `ResourceQuota`, `LimitRange` | Helm | `calculator` | Limits and policy | Both | Helm |
