# ruff: noqa: E501 - page text: each sentence stays on one line so a change shows as one line in a diff
"""Workflow files and stages of the pipeline page: one stage is one job of .github/workflows (or a part of a long job).

Each task names the YAML steps it covers; build.py checks them against the real workflow files.
"""

from typing import Any


def task(t: str, do: str, steps: str | list[str], i: str | None = None, always: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {"t": t, "do": do, "steps": steps if isinstance(steps, list) else [steps]}
    if i:
        d["if"] = i
    if always:
        d["always"] = True
    return d


def stage(file: str, job: str, name: str, meta: str, summary: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(file=file, job=job, name=name, meta=meta, summary=summary, tasks=tasks)


CI = "concurrency ci-<workflow>-<ref>, a newer run cancels the older"
DOCK = "the Dockerfile exists"
NDRY = "not a dry run"
DM = "ubuntu-latest, 120 min, environment: dev|prod, inputs: ignore_nat_window, dry_run"
XM = "ubuntu-latest, 120 min, environment: dev, concurrency deploy-dev (shared with Deploy DEV)"
CFN = "dev is owned by CloudFormation"
stages = {
    "ci_iac": stage(
        "ci.yml",
        "iac",
        "IaC and scripts",
        "ubuntu-latest, 15 min, Python 3.14; " + CI,
        "Checks every CloudFormation/SAM template, the deployment scripts and the two Lambda functions. No AWS access.",
        [
            task(
                "Check out, set up Python",
                "Gets the code and Python 3.14 with the pip cache.",
                ["Checkout Repository", "Setup Python"],
            ),
            task(
                "Install pinned tools",
                "Installs the dev requirements, cfn-lint, SAM CLI, helm (SHA-256 checked) and shellcheck.",
                ["Install tooling (pinned)", "Install helm (pinned, checksum verified) and shellcheck"],
            ),
            task(
                "Run shellcheck",
                "Lints every scripts/*.sh; any finding fails the job.",
                "Shellcheck (deployment scripts)",
            ),
            task(
                "Run cfn-lint",
                "Checks every templates/*.yaml against the us-east-2 resource rules.",
                "cfn-lint (every template)",
            ),
            task(
                "Run sam validate",
                "Runs sam validate --lint on every template, with the dev and then the prod configuration.",
                "SAM validate (lint), dev and prod",
            ),
            task(
                "Test scripts and templates",
                "Runs pytest in scripts/tests: deploy driver, guards, parameters and template rules.",
                "Deployment scripts and template tests",
            ),
            task(
                "Test NAT scheduler",
                "Runs pytest in src/scheduler; coverage under 85 percent fails.",
                "NAT scheduler tests (coverage at least 85 percent)",
            ),
            task(
                "Test rotation function",
                "Installs its requirements and runs pytest in src/rotation; coverage under 85 percent fails.",
                "Rotation function tests (coverage at least 85 percent)",
            ),
            task(
                "Run ruff and mypy",
                "Lint, format check and types on the rotation function, then on the scheduler and the listed deployment scripts.",
                ["Ruff and mypy on the rotation function", "Ruff and mypy on the scheduler and the deployment scripts"],
            ),
        ],
    ),
    "ci_core": stage(
        "ci.yml",
        "python-core",
        "Python core",
        "ubuntu-latest, 20 min, PostgreSQL 17.11 service; " + CI,
        "Checks the backend code (src/backend/calculator_core) with a real throwaway PostgreSQL.",
        [
            task(
                "Check out, set up Python",
                "Gets the code and Python 3.14; PostgreSQL is already healthy on port 5432.",
                ["Checkout Repository", "Setup Python"],
            ),
            task("Install dependencies", "Installs the pinned dev requirements.", "Install dependencies (pinned)"),
            task("Run ruff", "Lint and format check.", "Ruff (lint and format)"),
            task("Run mypy strict", "Strict type check.", "Mypy (strict)"),
            task(
                "Check layering", "lint-imports fails when a layer imports one it must not.", "Layering (import-linter)"
            ),
            task(
                "Run tests",
                "Runs pytest against PostgreSQL; coverage under 85 percent fails.",
                "Tests (coverage at least 85 percent, PostgreSQL service)",
            ),
            task(
                "Refuse skipped database tests",
                "Runs only the postgres tests again; fails if any was skipped or none passed.",
                "Fail if the PostgreSQL tests were skipped",
            ),
            task("Run pip check", "Fails on conflicting dependencies.", "Consistent dependencies (pip check)"),
            task(
                "Run pip-audit",
                "Fails on any known vulnerability of the runtime requirements, whatever its severity.",
                "Audit runtime dependencies (pip-audit)",
            ),
        ],
    ),
    "ci_chart": stage(
        "ci.yml",
        "chart",
        "Helm chart",
        "ubuntu-latest, 15 min; " + CI,
        "Lints and render-tests the application chart with placeholder values.",
        [
            task("Check out, set up Python", "Gets the code and Python 3.14.", ["Checkout Repository", "Setup Python"]),
            task("Install test tools", "Installs scripts/requirements-dev.txt.", "Install tooling (pinned)"),
            task(
                "Install helm",
                "Downloads helm v3.22.0 and checks its SHA-256.",
                "Install helm (pinned, checksum verified)",
            ),
            task(
                "Lint chart",
                "helm lint with values-dev and values-prod; prod also gets a placeholder web ACL.",
                "Helm lint (dev and prod)",
            ),
            task(
                "Run chart tests",
                "Runs pytest in chart/tests; a skipped test also fails the job.",
                "Chart tests (helm on PATH, none skipped)",
            ),
            task("Run ruff and mypy", "Lint, format and types on the chart tests.", "Ruff and mypy on the chart tests"),
        ],
    ),
    "ci_image": stage(
        "ci.yml",
        "image",
        "Container image",
        "ubuntu-latest, 20 min, needs python-core; linux/arm64 under QEMU; " + CI,
        "Builds the image and runs it on the runner. Nothing is pushed to a registry.",
        [
            task("Check out code", "Gets the code.", "Checkout Repository"),
            task(
                "Look for the Dockerfile",
                "Sets present=true or false; without src/backend/Dockerfile the rest is skipped with a notice.",
                "Check that the Dockerfile exists",
            ),
            task(
                "Set up QEMU and Buildx",
                "Lets the x86 runner build and run arm64 images.",
                ["Set up QEMU (arm64)", "Set up Docker Buildx"],
                DOCK,
            ),
            task(
                "Build image",
                "docker buildx build --load; the CA trust bundle is built by src/rotation/package_bundle.py.",
                "Build the image",
                DOCK,
            ),
            task(
                "Check non-root user",
                "Runs id in the image; it must be uid 10001 and gid 10001.",
                "The image runs as uid 10001 and gid 10001 (non-root)",
                DOCK,
            ),
            task(
                "Check dispatcher",
                "An unknown command must exit 2 with the usage; ingest without settings must exit 1 and name the problem.",
                "The dispatcher refuses unknown commands and an unconfigured worker",
                DOCK,
            ),
            task(
                "Check /health",
                "Starts the add service with a read-only filesystem; /health must answer 200 within about 60 seconds.",
                "The service starts and answers /health",
                DOCK,
            ),
            task(
                "Stop container",
                "Removes the container, also after a failure.",
                "Stop the container",
                "always, when the Dockerfile exists",
                True,
            ),
        ],
    ),
    "d_guard": stage(
        "deploy.yml",
        "deploy",
        "Guards and login",
        DM,
        "Stops before any change when the branch, configuration, account, hour or owner is wrong.",
        [
            task(
                "Check environment and branch",
                "Accepts dev (any branch) or prod from main only.",
                "Refuse unsupported environments and prod outside main",
            ),
            task("Check out code", "Gets the code.", "Checkout Repository"),
            task(
                "Check configuration",
                "Needs AWS_REGION, the two Cognito variables, WebOrigin in parameters/<env>.json, the 3 secrets, and WAF_ACL_ARN in prod.",
                "Validate environment configuration",
            ),
            task(
                "Set up Python and tools",
                "Python 3.14, dev requirements, cfn-lint and SAM CLI.",
                ["Setup Python", "Install tooling (pinned)"],
            ),
            task(
                "Log in to AWS (OIDC)",
                "Assumes the deployment role of the environment; no stored keys.",
                "Configure AWS Credentials",
            ),
            task(
                "Check AWS account",
                "Compares the caller account with the secret AWS_ACCOUNT_ID; a difference stops the run.",
                "Verify target AWS account",
            ),
            task(
                "Check NAT hours",
                "nat_window.py: dev may start 08:00-20:30 Panama, or any time with keep-up true or ignore_nat_window. prod always passes.",
                "NAT window preflight",
            ),
            task(
                "Check IaC owner",
                "iac_owner_guard.py: stops unless free or owned by CloudFormation; unreadable also stops. No Terraform state is configured today.",
                "Owner guard (CloudFormation only)",
            ),
            task(
                "Package rotation function",
                "Builds .build/rotation/db-rotation.zip with the CA bundle.",
                "Package the rotation function",
            ),
        ],
    ),
    "d_stacks": stage(
        "deploy.yml",
        "deploy",
        "Stacks and add-ons",
        DM,
        "Plan mode checks change sets and executes nothing; real mode deploys security through storage, then the cluster add-ons.",
        [
            task(
                "Check Kubernetes version",
                "eks_latest_version.py fails unless KubernetesVersion is the newest in standard support.",
                "Check the Kubernetes version (plan only)",
                "dry run only",
            ),
            task(
                "Dry-run change sets",
                "Creates each change set, applies the removal/replacement guard and deletes it again. The alarms stack is not included.",
                "Dry run of the stacks, change sets without executing (plan only)",
                "dry run only",
            ),
            task(
                "Deploy stacks",
                "security, network, registry, scheduler*, database, rotation, cluster, deploy, bastion*, storage (*dev only). Guard before each execute; locks the default security group; dev turns the NAT up; prod sets termination protection.",
                "Deploy the stacks (security through storage)",
                NDRY,
            ),
            task(
                "Install cluster add-ons",
                "CodeBuild runs helm_addons.sh: load balancer controller, operator RBAC, Cluster Autoscaler (chart SHA-256 checked). prod stops on an unaccepted autoscaler version skew.",
                "Install the cluster add-ons (CodeBuild, command addons)",
                NDRY,
            ),
        ],
    ),
    "d_image": stage(
        "deploy.yml",
        "deploy",
        "Image",
        DM,
        "Builds the arm64 image and pushes it with an immutable tag. All steps run only in a real deployment.",
        [
            task(
                "Set up QEMU and Buildx",
                "Lets the x86 runner build arm64.",
                ["Set up QEMU (arm64)", "Set up Docker Buildx"],
                NDRY,
            ),
            task(
                "Log in to ECR",
                "Reads the repository from the registry stack, sets the tag <commit>-<run id> and logs docker in.",
                "Log in to the image registry (ECR)",
                NDRY,
            ),
            task(
                "Build and push image",
                "Pushes the image and fails unless the ECR digest equals the build digest.",
                "Build and push the image (immutable tag, digest)",
                NDRY,
            ),
        ],
    ),
    "d_release": stage(
        "deploy.yml",
        "deploy",
        "Release and alarms",
        DM,
        "Migrates the database, upgrades the Helm release by digest, then deploys the alarms. There is no smoke test against the deployed API.",
        [
            task(
                "Apply migration policy and Job",
                "CodeBuild runs helm_deploy.sh: it renders and applies the namespace, the service accounts, the migration network policy (egress of the Job; the release adopts it) and the migrate Job, then waits up to 16 minutes for the Job.",
                "Deploy the release (CodeBuild, command deploy)",
                NDRY,
            ),
            task(
                "Upgrade release, wait rollouts",
                "Same build: helm upgrade --install --atomic --wait by image digest (10 min; a failure rolls back to the previous release), then waits for the rollout of every deployment.",
                "Deploy the release (CodeBuild, command deploy)",
                NDRY,
            ),
            task(
                "Publish API registration",
                "Same build: waits up to 5 minutes for the load balancer address, then writes and tags the dns-name and hosted-zone-id parameters of the shared edge in SSM.",
                "Deploy the release (CodeBuild, command deploy)",
                NDRY,
            ),
            task(
                "Find alarm targets",
                "Finds the node group, load balancer and target groups by tag, enables the group metric and builds the weekly reminder text.",
                "Discover the alarm dimensions and build the reminder",
                NDRY,
            ),
            task(
                "Deploy alarms stack",
                "cfn_deploy.py --only alarms, with the same change set guard.",
                "Deploy the alarms stack",
                NDRY,
            ),
            task(
                "Write next steps",
                "Summary: confirm the SNS e-mail; in dev also run Deploy DEV in shared-resources.",
                "Deployment summary",
                "success and not a dry run",
            ),
        ],
    ),
    "x_guard": stage(
        "destroy-dev.yml",
        "destroy",
        "Guards and owner",
        XM,
        "Checks the branch, the typed name, the account and what owns dev. Nothing is changed.",
        [
            task("Check out code", "Gets the code.", "Checkout Repository"),
            task(
                "Check branch and confirm text",
                "Only from develop; mode destroy needs the repository name typed in confirm.",
                "Validate the request",
            ),
            task(
                "Check configuration",
                "Needs AWS_REGION and the secrets ROLE_ARN and AWS_ACCOUNT_ID.",
                "Validate environment configuration",
            ),
            task("Set up Python", "Python 3.14.", "Setup Python"),
            task("Log in to AWS (OIDC)", "Assumes the dev deployment role.", "Configure AWS Credentials"),
            task("Check AWS account", "A different account stops the run.", "Verify target AWS account"),
            task(
                "Detect owner",
                "iac_owner_guard.py detect: cloudformation or none; both or unreadable fails closed.",
                "Detect the deployed owner",
            ),
            task(
                "Report owner",
                "Prints owner and IAC_TOOL (information only); owner terraform would stop here, but no Terraform state is configured today.",
                "Owner and IAC_TOOL",
            ),
            task(
                "Report nothing deployed",
                "Writes that dev is empty in the summary.",
                "Nothing is deployed",
                "owner is none",
            ),
        ],
    ),
    "x_run": stage(
        "destroy-dev.yml",
        "destroy",
        "Plan and destroy",
        XM,
        "Always lists what would go; removes it only in mode destroy, then checks for leftovers.",
        [
            task(
                "Plan destruction",
                "Lists each stack in reverse order: resources, what is retained, what blocks a delete. Read-only.",
                "Plan the destruction (CloudFormation)",
                CFN,
            ),
            task(
                "Destroy",
                "NAT up, CodeBuild helm_uninstall.sh, wait (20 min) for the load balancer to go, remove the SSM registration, then per stack: protection off, empty buckets/ECR, delete, wait. Last: RDS Proxy log group.",
                "Destroy (CloudFormation)",
                "mode is destroy and " + CFN,
            ),
            task(
                "Write closing notice",
                "Reminds to run Deploy DEV in shared-resources and set API_WEIGHT_EKS to 0.",
                "Closing notice for the owner",
                "mode is destroy and " + CFN,
            ),
            task(
                "Check for leftovers",
                "post_destroy_check.py: up to 4 passes, 60 s apart; fails if anything unexpected is left.",
                "Verify nothing is left behind",
                "mode is destroy",
            ),
        ],
    ),
    "k_nat": stage(
        "keep-nat.yml",
        "keep-nat",
        "Keep NAT (dev)",
        "ubuntu-latest, 10 min, environment: dev, concurrency keep-nat-dev",
        "Writes one SSM parameter that the NAT scheduler reads before the 22:00 shutdown.",
        [
            task(
                "Check configuration",
                "Needs AWS_REGION and the secrets ROLE_ARN and AWS_ACCOUNT_ID.",
                "Validate environment configuration",
            ),
            task("Log in to AWS (OIDC)", "Assumes the dev deployment role.", "Configure AWS Credentials"),
            task("Check AWS account", "A different account stops the run.", "Verify target AWS account"),
            task(
                "Write keep-up setting",
                "put-parameter --overwrite sets /oecalc/dev/eks/nat-keep-up to true or false and writes the message in the summary. It does not check first that the parameter exists.",
                "Write the keep-up setting",
            ),
        ],
    ),
}
files = {
    "ci.yml": {
        "role": "Quality gates: templates, scripts, backend, chart and image. It never touches AWS.",
        "triggers": [
            "every pull request",
            "push to any branch or tag (no paths filter)",
            "called by deploy-dev.yml and deploy-prod.yml",
        ],
    },
    "deploy-dev.yml": {
        "role": "Entry point of dev: runs CI, then the deployment.",
        "triggers": ["manual (input ignore_nat_window); no push trigger on purpose"],
    },
    "deploy-prod.yml": {
        "role": "Entry point of prod: CI, a dry-run plan, then the real deployment. Only from main.",
        "triggers": ["push to main", "manual, effective from main only"],
    },
    "deploy.yml": {
        "role": "The reusable deployment for dev and prod (one job).",
        "triggers": ["called by deploy-dev.yml (once) and deploy-prod.yml (dry run, then real)"],
    },
    "destroy-dev.yml": {
        "role": "Manual teardown of dev: plan by default, typed confirmation to destroy. prod has no destroy workflow.",
        "triggers": ["manual (inputs mode, confirm), from develop only"],
    },
    "keep-nat.yml": {
        "role": "Keeps the dev NAT gateway up past 22:00 Panama, or restores the schedule.",
        "triggers": ["manual (input keep)"],
    },
}
