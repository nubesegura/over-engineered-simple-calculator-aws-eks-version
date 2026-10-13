# over-engineered-simple-calculator-aws-eks-version

A deliberately over-engineered calculator backend on Amazon EKS (managed node group, arm64), the Kubernetes sibling of
the `sls` and `ecs` versions: same API contract, different infrastructure, built to learn EKS. Four arithmetic
operations and a history service run as separate deployments behind an internet-facing Application Load Balancer
created from a Kubernetes Ingress; the history lives in Amazon RDS for PostgreSQL behind RDS Proxy; a worker loads CSV
files dropped in an S3 bucket. The web page does not change: traffic reaches this backend when its weight is set in the
shared Route 53 edge (`over-engineered-simple-calculator-shared-resources`).

Infrastructure is AWS SAM / CloudFormation (`templates/`, `parameters/{dev,prod}.json`); workloads are a Helm chart
(`chart/`) installed by a CodeBuild project inside the VPC, because the cluster endpoint is private. `dev` is deployed
and meant to be destroyed when the study is over; `prod` is designed to the same templates and chart, checked offline and
not deployed.

Status: baseline (spec `001-create-baseline`) implemented and running in `dev`.

## Documentation

| Document | Content |
|---|---|
| [docs/functional.md](docs/functional.md) | What the product does: API contract, errors, alerts, CSV ingestion, behaviour of the environment, moving traffic, retirement |
| [docs/technical.md](docs/technical.md) | Stack, configuration, database, edge, network policies, deployment workflows, logs and runbooks (bastion, pod logs, debugging path from DNS to the database, certificate rule, shared edge, destroy order) |
| [docs/architecture.md](docs/architecture.md) | Components, design decisions D1 to D35, resource inventory, known limits |
| [docs/adr/](docs/adr/README.md) | Architecture decision records |
| Diagrams | AWS components: [docs/architecture.drawio](docs/architecture.drawio), [docs/architecture.png](docs/architecture.png). Layers: [docs/layers.drawio](docs/layers.drawio), [docs/layers.png](docs/layers.png) |
| [templates/README.md](templates/README.md), [chart/README.md](chart/README.md) | Stack layout and rules that are easy to break; chart notes |

## Quick start

Local checks (no AWS access needed), from the repository root:

```
pip install -r scripts/requirements-dev.txt cfn-lint==1.48.1
cfn-lint --regions us-east-2 -- templates/*.yaml
python -m pytest scripts/tests
python -m pytest chart/tests -q                # render tests need helm on the path
cd src/backend/calculator_core && pip install -r requirements-dev.txt \
  && ruff check . && ruff format --check . && mypy && lint-imports && python -m pytest
```

Deploy `dev` (GitHub Actions, OIDC; names only, values are in the GitHub environment):

1. In the GitHub environment `dev` set the secrets `ROLE_ARN`, `AWS_ACCOUNT_ID`, `SUPPORT_EMAIL` and the variables
   `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`. The shared edge (certificate, hostname) and the Cognito
   pool must already exist.
2. Run the workflow **Deploy DEV** from `develop`, between 08:00 and about 20:30 Panama time (the NAT gateway is removed
   at night). It checks, deploys the stacks, installs the controllers, builds the image, installs the release and
   registers the load balancer.
3. Confirm the email subscription of the alerts topic, then run **Deploy DEV** in `shared-resources` and set
   `API_WEIGHT_EKS` there to send traffic to this backend.
4. Open a read-only session on the bastion to look inside the cluster (see the runbooks in
   [docs/technical.md](docs/technical.md)).
5. When finished, run **Destroy DEV** in `plan` mode, then in `destroy` mode typing the repository name.

`prod` deploys only from `main` with **Deploy PROD**: a plan job (dry run, nothing executed) and then the apply job. Read
the production checklist in [docs/technical.md](docs/technical.md) first. There is no workflow that destroys `prod`.

## Repository layout

```
src/backend/calculator_core/   Python package (domain, application, adapters, config) and its tests
src/backend/Dockerfile         one image for every workload
src/backend/migrations/        versioned SQL, applied by the migrate job
src/rotation/, src/scheduler/  Lambda functions: credential rotation, NAT scheduler
templates/, parameters/        CloudFormation / SAM stacks and one parameter file per environment
chart/, chart-addons/          Helm chart of the calculator; values for the load balancer controller and the autoscaler
scripts/                       deployment, destroy and verification scripts, with tests
.github/workflows/             ci, deploy-dev, deploy-prod, deploy, destroy-dev, keep-nat; dependabot.yml
docs/                          product documentation
```

Everything is English. No secrets, tokens or account identifiers are stored in this repository.
