# over-engineered-simple-calculator-aws-eks-version

A deliberately over-engineered calculator backend on Amazon EKS (managed node group, arm64), the Kubernetes
sibling of the `sls` and `ecs` versions: same API contract, different infrastructure, built to learn EKS.
Infrastructure is AWS SAM / CloudFormation (`templates/`, `parameters/{dev,prod}.json`); workloads are a Helm
chart installed by a CodeBuild project inside the VPC. Only `dev` is deployed; `prod` is designed and checked
offline. Everything is meant to be destroyed when the study is over.

Status: baseline in progress (milestone 1: network, keys, repository). See `templates/README.md` for the
stack layout and the checks that run in CI.

Local checks (no AWS access needed):

```
pip install -r scripts/requirements-dev.txt cfn-lint==1.48.1
cfn-lint --regions us-east-2 -- templates/*.yaml
python -m pytest scripts/tests
```
