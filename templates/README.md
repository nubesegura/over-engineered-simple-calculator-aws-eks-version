# Templates

One CloudFormation (or SAM) template per stack, identical across environments. Everything that differs
between `dev` and `prod` is in `parameters/<env>.json` (`Parameters` and the five mandatory `Tags`); both
files must keep the same keys (`scripts/tests/test_parameters.py`). `stacks.json` lists the stacks in deploy
order (destroy runs it in reverse). A unit with `onlyIf` is deployed only when that parameter has that value
(`bastion` needs `EnableBastion=true`, `scheduler` needs `NatSchedulerEnabled=true`).

Stack names: `stk-<RegionCode>-<Context>-eks-<unit>-<EnvType>`. Data crosses stacks with `Outputs` and
`Export` named `<stack name>:<OutputName>`, read with `Fn::ImportValue`.

| Unit | Template | Notes |
|---|---|---|
| security | `security.yaml` | Data key and cluster secrets key (aliases `alias/kms-<region>-<context>-eks[-secrets]-<env>`), alerts topic. Exports `DataKeyAlias`, `SecretsKeyAlias`, `AlertsTopicArn`, plus the two key ARNs for services that accept nothing else (log groups, cluster encryption). |
| network | `network.yaml` | VPC, subnets, route tables, S3 gateway endpoint, security groups. In `dev` no NAT gateway and no private default route (the scheduler owns them); in `prod` one NAT gateway per zone and a private route table per zone. Prod only: flow logs. |
| registry | `registry.yaml` | One ECR repository `ecr-<region>-<context>-eks-<env>`. |
| scheduler, database, rotation, cluster, deploy, bastion, storage, alarms | `scheduler.yaml`, `database.yaml`, `rotation.yaml`, `cluster.yaml`, `deploy.yaml`, `bastion.yaml`, `storage.yaml`, `alarms.yaml` | NAT schedule and function; database, proxy and secrets; credential rotation; EKS cluster, node group, add-ons and pod identity roles; deployment project; bastion; ingestion bucket, queues and ALB logs; alarms and notices. See `docs/architecture.md` for the resource table. |

## Rules that are easy to break

- Security group descriptions are replacement-forcing: never edit them (frozen in `scripts/tests/test_templates_m1.py`).
- The VPC range is a parameter; ranges must not overlap the plan in `docs/architecture.md` D23 (`scripts/tests/test_cidr_plan.py`).
- Key use is granted in IAM policies by alias (`kms:ResourceAliases` plus `kms:ViaService`), never by key ARN; no workload role gets alias management actions.
- The ECR repository policy names the node role (`iamr-<region>-<context>-eks-node-<env>`) and the deployment build role (`iamr-<region>-<context>-eks-deploy-<env>`) in a condition; the cluster and deploy stacks must create roles with exactly those names.
- Encrypted node volumes need the key policy to name the Auto Scaling service-linked role, which must exist (a key policy that names a missing role fails). `AutoScalingServiceLinkedRoleExists` is `true` in dev. In prod it is `false`: run `aws iam create-service-linked-role --aws-service-name autoscaling.amazonaws.com` once, then set it to `true` before deploying `cluster`.
- **Accepted deviation of the naming table:** CloudWatch alarms are named `alm-...` and EventBridge Scheduler schedules `sch-...`, while the table says `alrm` and `evs`. Renaming either is a replacement, so the names stay as they are. If the project is ever destroyed and recreated, create these resources with `alrm-` and `evs-` from the start, so no replacement is needed: alarms in `alarms.yaml` (17 alarms) and `rotation.yaml` (database rotation errors), alarm and schedules in `scheduler.yaml` (`nat-errors`, `nat-down`, `nat-up`), schedule in `alarms.yaml` (`reminder`). `database.yaml` has no alarms or schedules. Change the names in the templates and in the tests that read them in the same change.
- Values the pipeline injects and that are not in the parameter files: `SupportEmail` (secret) and `WafAclArn` (prod, empty in the file).
- **The Cluster Autoscaler cannot work while the NAT gateway is removed.** The private subnets reach the Auto Scaling and EC2 APIs only through the NAT gateway (the only VPC endpoint is the S3 gateway one), and new nodes need it to join and pull images. In `dev` outside the scheduler window (22:00-08:00 Panama time) nodes cannot be added, so run load tests while the NAT gateway exists. Node limits (2 to 4) are the node group's own `NodeMinSize`/`NodeMaxSize`; the autoscaler discovers the group by the tags `k8s.io/cluster-autoscaler/enabled` and `k8s.io/cluster-autoscaler/<cluster name>` (`chart-addons/cluster-autoscaler.values.yaml`).
