# ruff: noqa: E501 - page text: each sentence stays on one line so a change shows as one line in a diff
"""Scenarios of the pipeline page: which stages run, in which order and how each one ends, per trigger."""

from typing import Any

from stages import files, stages

sc: list[dict[str, Any]] = []


def ci(
    via: str,
    fail: str | None = None,
    at: int | None = None,
    note: str | None = None,
    cancel: bool = False,
    skipall: bool = False,
    skip: list[int] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, s, lane in (
        ("core", "ci_core", 0),
        ("iac", "ci_iac", 1),
        ("chart", "ci_chart", 2),
        ("image", "ci_image", 0),
    ):
        d: dict[str, Any] = {"id": i, "stage": s, "lane": lane, "via": via}
        if i == "image":
            d["after"] = ["core"]
        if skipall:
            d["outcome"] = "skipped"
            d["note"] = "Skipped: the ref is not main."
        elif cancel:
            d["outcome"] = "canceled" if i != "image" else "skipped"
            d["note"] = (
                "Cancelled: a newer run of the same workflow and ref replaced this one."
                if i != "image"
                else "Never starts: the run was cancelled."
            )
        if cancel and i != "image":
            d["skip"] = {"core": [5, 6, 7, 8], "iac": [4, 5, 6, 7, 8], "chart": [3, 4, 5]}[i]
            d["name"] = {"core": "Python core", "iac": "IaC, scripts", "chart": "Helm chart"}[i] + " (cancelled)"
        if i == fail:
            d.update(outcome="failed", failAt=at, note=note)
        elif i == "image" and fail == "core":
            d.update(outcome="skipped", note="Skipped: it needs python-core.")
        if i == "image" and skip:
            d["skip"] = skip
            d["note"] = note
        out.append(d)
    return out


def dep(
    via: str,
    fail: str | None = None,
    at: int | None = None,
    note: str | None = None,
    after: tuple[str, ...] = ("iac", "chart", "image"),
    run: str | None = None,
    plan: bool = False,
    skipped: bool = False,
) -> list[dict[str, Any]]:
    pre = run + "_" if run else ""
    out: list[dict[str, Any]] = []
    prev = list(after)
    seq = ["guard", "stacks"] if plan else ["guard", "stacks", "image", "release"]
    dead = skipped
    for n in seq:
        d: dict[str, Any] = {"id": pre + "d" + n, "stage": "d_" + n, "lane": 0, "after": prev, "via": via}
        if run:
            d["run"] = run
            d["name"] = {"plan": "Plan: ", "apply": "Apply: "}[run] + {
                "guard": "guards",
                "stacks": "change sets" if plan else "stacks, add-ons",
                "image": "image",
                "release": "release, alarms",
            }[n]
        if n == "stacks":
            d["skip"] = [2, 3] if plan else [0, 1]
        if n == "guard":
            d["note"] = "Every guard passes; AWS is reachable with a short-lived role."
        if n == "stacks" and plan:
            d["note"] = (
                "Change sets of the 8 prod stacks pass the guard and are deleted. The image, release and alarms steps of the job are skipped (dry run)."
            )
        if dead:
            d["outcome"] = "skipped"
            d["note"] = "Skipped."
            d.pop("skip", None)
        elif fail == n:
            d.update(outcome="failed", failAt=at, note=note)
            dead = True
        out.append(d)
        prev = [d["id"]]
    return out


def add(
    id: str,
    group: str,
    title: str,
    desc: str,
    res: str,
    ttype: str,
    tt: str,
    ref: str,
    inputs: list[str],
    entry: str,
    st: list[dict[str, Any]],
) -> None:
    sc.append(
        dict(
            id=id,
            group=group,
            title=title,
            desc=desc,
            result=res,
            trigger=dict(type=ttype, title=tt, ref=ref, inputs=inputs, entry=entry),
            stages=st,
        )
    )


def c(id: str, title: str, desc: str, res: str, st: list[dict[str, Any]], t: str = "pr") -> None:
    add(
        id,
        "Pull requests and pushes (CI)",
        title,
        desc,
        res,
        t,
        "Pull request" if t == "pr" else "Push",
        "any branch",
        [],
        "ci.yml",
        st,
    )


c(
    "ci-pr",
    "Pull request, all green",
    "Any pull request, including the weekly grouped Dependabot ones. A branch pushed in this repository also starts the push run, so CI runs twice.",
    "All gates green. Nothing is deployed.",
    ci("ci.yml"),
)
c(
    "ci-push",
    "Push to any branch or tag, green",
    "Any push, also to develop and also docs-only changes: there is no paths filter. A push to develop does not deploy; a push to main also starts Deploy PROD, which runs CI again.",
    "All gates green. Nothing is deployed by this workflow.",
    ci("ci.yml"),
    "push",
)
c(
    "ci-iac",
    "Bad template: IaC gate fails",
    "A template breaks a cfn-lint rule.",
    "The CI run is red; the other jobs still finish.",
    ci("ci.yml", "iac", 3, "cfn-lint rejects a template; the later tasks of this job do not run."),
)
c(
    "ci-scr",
    "Script tests fail",
    "A deployment script, guard or template rule test fails in scripts/tests.",
    "The CI run is red; the other jobs still finish.",
    ci("ci.yml", "iac", 5, "pytest fails in scripts/tests."),
)
c(
    "ci-core",
    "Backend tests fail",
    "A backend test fails or coverage drops under 85 percent.",
    "Red; the image is not built.",
    ci("ci.yml", "core", 5, "pytest fails, so the image job is skipped."),
)
c(
    "ci-pg",
    "Database tests were skipped",
    "The PostgreSQL service or TEST_DATABASE_URL does not work, so the database tests skip themselves.",
    "Red on purpose: a hidden skip counts as a failure. No image.",
    ci("ci.yml", "core", 6, "A skipped PostgreSQL test fails the job."),
)
c(
    "ci-audit",
    "Vulnerable dependency found",
    "pip-audit finds a known vulnerability in a runtime requirement.",
    "Red until the owner updates or reviews it. No image.",
    ci("ci.yml", "core", 8, "pip-audit fails on any finding."),
)
c(
    "ci-chart",
    "Chart gate fails",
    "helm lint or a chart test fails, or the chart tests were skipped.",
    "Red; the other jobs still finish.",
    ci("ci.yml", "chart", 4, "A chart test fails or is skipped."),
)
c(
    "ci-health",
    "Image does not answer /health",
    "The image builds but the service does not start.",
    "Red; the container is still removed.",
    ci(
        "ci.yml",
        "image",
        6,
        "/health never answers 200: logs are printed and the job fails; the container is removed anyway.",
    ),
)
c(
    "ci-root",
    "Image runs as the wrong user",
    "The image does not use uid and gid 10001.",
    "Red; nothing was pushed.",
    ci("ci.yml", "image", 4, "The user check fails; the later checks do not run."),
)
c(
    "ci-nodocker",
    "No Dockerfile in the branch",
    "A branch without src/backend/Dockerfile.",
    "Green: the image checks are skipped with a notice.",
    ci("ci.yml", skip=[2, 3, 4, 5, 6, 7], note="The Dockerfile is missing: a notice and every image check skipped."),
)
c(
    "ci-cancel",
    "Newer push cancels this run",
    "A second push to the same ref arrives while CI runs.",
    "Cancelled; the newer run gives the verdict.",
    ci("ci.yml", cancel=True),
    "push",
)


def dev(
    id: str,
    title: str,
    desc: str,
    res: str,
    ign: str = "false",
    st: list[dict[str, Any]] | None = None,
    gn: str | None = None,
    **k: Any,
) -> None:
    st = st or ci("deploy-dev.yml > ci.yml") + dep("deploy-dev.yml > deploy.yml", **k)
    if gn:
        st[4]["note"] = gn
    add(
        id,
        "Deploy DEV",
        title,
        desc,
        res,
        "manual",
        "Run Deploy DEV",
        "chosen branch",
        ["ignore_nat_window = " + ign],
        "deploy-dev.yml",
        st,
    )


dev(
    "dev-ok",
    "Deploy DEV, all green",
    "Started by hand between 08:00 and 20:30 Panama. No approvers exist. Waits while another deploy or a destroy of dev runs (concurrency deploy-dev). No branch check: the chosen branch is what gets deployed.",
    "dev deployed and registered in SSM. Next by hand: confirm the SNS e-mail, run Deploy DEV in shared-resources. NAT goes down at 22:00.",
)
dev(
    "dev-ignore",
    "Outside the NAT hours, ignored",
    "Started outside 08:00-20:30 with ignore_nat_window = true.",
    "Deploys. The scheduler unit turns the NAT up during the run; a 22:00 shutdown in the middle can break the cluster or release part.",
    "true",
    gn="Outside the window, but the flag is true: the preflight passes.",
)
dev(
    "dev-keep",
    "Outside the NAT hours, NAT kept up",
    "Started outside 08:00-20:30 after Keep NAT = true.",
    "Deploys; the NAT stays up until Keep NAT = false.",
    gn="Outside the window, but nat-keep-up reads true: the preflight passes.",
)
dev(
    "dev-window",
    "Outside the NAT hours: refused",
    "Started outside 08:00-20:30 without the flag or keep-up (an unreadable parameter counts as false).",
    "Red, nothing changed. Wait, run Keep NAT, or use ignore_nat_window.",
    fail="guard",
    at=6,
    note="nat_window.py refuses before the owner check and before any change.",
)
dev(
    "dev-ci",
    "CI is red: no deployment",
    "A gate fails on the chosen branch.",
    "Red; the deploy job is skipped; AWS is never reached.",
    st=ci("deploy-dev.yml > ci.yml", "core", 5, "pytest fails.") + dep("deploy-dev.yml > deploy.yml", skipped=True),
)
dev(
    "dev-conf",
    "Missing variable or secret",
    "A value is missing in the dev GitHub environment, or WebOrigin is empty.",
    "Red before the AWS login; every missing name is listed.",
    fail="guard",
    at=2,
    note="The missing names are printed and the job stops before AWS.",
)
dev(
    "dev-acct",
    "Wrong AWS account",
    "The role belongs to another account than the secret says.",
    "Red, nothing changed.",
    fail="guard",
    at=5,
    note="The caller account differs from AWS_ACCOUNT_ID.",
)
dev(
    "dev-owner",
    "Owner cannot be read",
    "describe-stacks fails (permission, network) so the owner is unknown.",
    "Red (fails closed), nothing changed.",
    fail="guard",
    at=7,
    note="The owner guard exits 4: unreadable means stop.",
)
dev(
    "dev-cs",
    "Change set would remove or replace",
    "A template change removes or replaces a resource that is not on the unit's allow list.",
    "Red. Earlier stacks stay deployed; this change set is left for review, not executed.",
    fail="stacks",
    at=2,
    note="The guard refuses the change set; later stacks, image and release do not run.",
)
dev(
    "dev-stack",
    "A stack fails",
    "CloudFormation fails while executing a change set.",
    "Red. CloudFormation rolls that stack back; earlier stacks stay. The next run deletes a ROLLBACK_COMPLETE stack and creates it again; *_FAILED needs a manual look.",
    fail="stacks",
    at=2,
    note="The failed events are printed; no later stack runs.",
)
dev(
    "dev-addons",
    "Add-ons build fails",
    "A chart checksum differs, the NAT is down, or a controller does not become ready.",
    "Red. Stacks stay; helm --atomic undoes the failing chart. Run Deploy DEV again.",
    fail="stacks",
    at=3,
    note="The CodeBuild build fails; its last 60 log lines are in the summary.",
)
dev(
    "dev-img",
    "Image push or digest fails",
    "The build or push fails, or ECR reports another digest.",
    "Red. The running release is untouched.",
    fail="image",
    at=2,
    note="No digest is exported, so no release starts.",
)
dev(
    "dev-mig",
    "Database migration fails",
    "The migrate Job fails or takes over 16 minutes (for example it cannot reach the database or AWS).",
    "Red. The previous release keeps serving; the new image stays in ECR. The migration is not reverted by this workflow.",
    fail="release",
    at=0,
    note="The policy and the Job are applied, the Job fails: its logs are printed and the release is not touched.",
)
dev(
    "dev-helm",
    "Rollout fails and rolls back",
    "New pods do not become ready within 10 minutes.",
    "Red. helm --atomic restores the previous release. The migration already applied stays. The alarms are not updated.",
    fail="release",
    at=1,
    note="helm upgrade fails and rolls back; history, pods and events are printed.",
)
dev(
    "dev-alb",
    "No load balancer address",
    "The release is healthy but the Ingress has no address after 5 minutes.",
    "Red. The release stays installed (no rollback here); nothing is registered in SSM.",
    fail="release",
    at=2,
    note="The release is up, but the registration is not published.",
)
dev(
    "dev-disc",
    "Alarm targets not found",
    "The node group, load balancer or a target group is missing or duplicated by tag.",
    "Red. The application is live and registered; the alarms stack is not updated.",
    fail="release",
    at=3,
    note="discover_targets.py stops; no rollback of the release.",
)
dev(
    "dev-alarm",
    "Alarms stack fails",
    "The alarms change set is refused by the guard or fails to execute.",
    "Red. The application is live; a failed execution is rolled back by CloudFormation.",
    fail="release",
    at=4,
    note="Only the alarms stack is affected.",
)
V = "deploy-prod.yml > "


def pr(
    id: str,
    title: str,
    desc: str,
    res: str,
    ttype: str = "push",
    tt: str = "Push to main",
    ref: str = "main",
    c_: list[dict[str, Any]] | None = None,
    pf: tuple[str, int, str] | None = None,
    af: tuple[str, int, str] | None = None,
    allskip: bool = False,
    skipdep: bool = False,
) -> None:
    st = c_ or ci(V + "ci.yml", skipall=allskip)
    pk: dict[str, Any] = dict(fail=pf[0], at=pf[1], note=pf[2]) if pf else {}
    st = st + dep(V + "deploy.yml", run="plan", plan=True, skipped=allskip or skipdep, **pk)
    ak: dict[str, Any] = dict(fail=af[0], at=af[1], note=af[2]) if af else {}
    st = st + dep(V + "deploy.yml", run="apply", after=("plan_dstacks",), skipped=allskip or skipdep or bool(pf), **ak)
    add(id, "Deploy PROD", title, desc, res, ttype, tt, ref, [], "deploy-prod.yml", st)


pr(
    "prod-ok",
    "Push to main, all green",
    'A merge of develop into main. No approvers: the dry run and the guard are the safety. One run at a time (concurrency deploy-prod). CI also runs separately for the same push. Needs the autoscaler skew solved (see "As configured today"); prod was never deployed.',
    "prod deployed and registered. Stacks get termination protection.",
)
pr(
    "prod-man",
    "Manual run from main",
    "Same path, started by hand from main.",
    "Same as a push to main.",
    "manual",
    "Run Deploy PROD",
)
pr(
    "prod-branch",
    "Manual run from another branch",
    "Started by hand from a branch that is not main.",
    "Nothing runs: every job is skipped by its condition. The run is not red.",
    "manual",
    "Run Deploy PROD",
    "not main",
    allskip=True,
)
pr(
    "prod-ci",
    "CI is red: no plan",
    "A gate fails on main.",
    "Red; plan and apply are skipped.",
    c_=ci(V + "ci.yml", "image", 6, "/health fails."),
    skipdep=True,
)
pr(
    "prod-conf",
    "Missing prod configuration",
    "For example WAF_ACL_ARN is not set in the prod environment.",
    "Red before the AWS login; apply is skipped.",
    pf=("guard", 2, "The missing names are printed; nothing reaches AWS."),
)
pr(
    "prod-acct",
    "Wrong AWS account",
    "The role points to another account.",
    "Red, nothing changed; apply is skipped.",
    pf=("guard", 5, "The caller account differs from AWS_ACCOUNT_ID."),
)
pr(
    "prod-owner",
    "Owner cannot be read",
    "describe-stacks fails, so the owner is unknown.",
    "Red (fails closed); apply is skipped.",
    pf=("guard", 7, "The owner guard exits 4."),
)
pr(
    "prod-k8s",
    "Kubernetes version is not newest",
    "AWS offers a newer version in standard support than parameters/prod.json.",
    "Red: update KubernetesVersion first. Nothing changed; apply is skipped.",
    pf=("stacks", 0, "eks_latest_version.py fails the plan."),
)
pr(
    "prod-cs",
    "Dry run: change set refused",
    "A change set would remove or replace something, or a stack is in ROLLBACK_COMPLETE.",
    "Red. Nothing executed; apply is skipped.",
    pf=(
        "stacks",
        1,
        "The dry run fails. On a first deployment, stacks that depend on one still to be created are only warned about.",
    ),
)
pr(
    "prod-skew",
    "As configured today: autoscaler skew",
    "Kubernetes 1.37, autoscaler v1.36.1 and AllowAutoscalerMinorSkew = false in parameters/prod.json. The plan does not check this.",
    "Red. The 8 prod stacks are already created; no add-ons, no application. Fix the parameter or the chart, then run again.",
    af=("stacks", 3, "helm_addons.sh stops before installing anything in prod."),
)
pr(
    "prod-stack",
    "A stack fails in the real run",
    "CloudFormation fails although the plan passed (the apply creates new change sets).",
    "Red. That stack rolls back; earlier ones stay.",
    af=("stacks", 2, "The failed events are printed; nothing later runs."),
)
pr(
    "prod-mig",
    "Migration fails in prod",
    "The migrate Job fails.",
    "Red. The previous release keeps serving; the stack changes stay.",
    af=("release", 0, "The release is not touched."),
)
pr(
    "prod-helm",
    "Rollout fails and rolls back",
    "New pods are not ready within 10 minutes.",
    "Red. The previous release is restored; the migration and the stack changes stay.",
    af=("release", 1, "helm --atomic rolls back."),
)


def ds(
    id: str,
    title: str,
    desc: str,
    res: str,
    mode: str = "plan",
    confirm: str = "",
    g: tuple[int, str] | None = None,
    r: tuple[int, str] | None = None,
    gs: tuple[int, ...] = (8,),
    rs: tuple[int, ...] = (),
    ref: str = "develop",
    rnote: str | None = None,
) -> None:
    a: dict[str, Any] = {"id": "g", "stage": "x_guard", "lane": 0, "via": "destroy-dev.yml", "skip": list(gs)}
    b: dict[str, Any] = {
        "id": "r",
        "stage": "x_run",
        "lane": 0,
        "after": ["g"],
        "via": "destroy-dev.yml",
        "skip": list(rs),
    }
    if g:
        a.update(outcome="failed", failAt=g[0], note=g[1])
        b.update(outcome="skipped", note="Never reached.")
        b.pop("skip")
    if r:
        b.update(outcome="failed", failAt=r[0], note=r[1])
    elif rnote and not g:
        b["note"] = rnote
    add(
        id,
        "Destroy DEV",
        title,
        desc,
        res,
        "manual",
        "Run Destroy DEV",
        ref,
        ["mode = " + mode] + (["confirm = " + confirm] if confirm else []),
        "destroy-dev.yml",
        [a, b],
    )


N = "<repository name>"
ds(
    "x-plan",
    "Plan only (default)",
    "See what a destroy would remove. Queues while a deploy of dev runs.",
    "Green; a full list in the summary; nothing deleted.",
    rs=(1, 2, 3),
    rnote="Only the read-only plan runs.",
)
ds(
    "x-plan0",
    "Plan when nothing is deployed",
    "No stack of this repository exists.",
    "Green; the summary says nothing is deployed.",
    gs=(),
    rs=(0, 1, 2, 3),
    rnote="Owner is none: every task is skipped.",
)
ds(
    "x-ok",
    "Destroy, all green",
    "Mode destroy with the repository name typed. The NAT gateway is recreated just for the uninstall; keep-up is ignored at the end.",
    "dev is gone. Next by hand: Deploy DEV in shared-resources and API_WEIGHT_EKS = 0. The SAM artifact bucket stays.",
    "destroy",
    N,
)
ds(
    "x-none",
    "Destroy when nothing is deployed",
    "Mode destroy but no stack exists.",
    "Green if clean. The leftover check still runs, so it goes red if, for example, the SSM registration remains (it is not removed on this path).",
    "destroy",
    N,
    gs=(),
    rs=(0, 1, 2),
    rnote="Owner is none: only the leftover check runs.",
)
ds(
    "x-text",
    "Wrong or empty confirmation text",
    "Mode destroy without the exact repository name.",
    "Red, nothing touched.",
    "destroy",
    "wrong or empty",
    g=(1, "The typed text is not the repository name; AWS is never reached."),
)
ds(
    "x-branch",
    "Not from develop",
    "Started from another branch, in either mode.",
    "Red, nothing touched.",
    ref="not develop",
    g=(1, "Only develop may destroy dev."),
)
ds(
    "x-conf",
    "Missing configuration",
    "AWS_REGION or a secret is missing in the dev environment.",
    "Red before AWS.",
    g=(2, "The missing names are printed."),
)
ds(
    "x-acct",
    "Wrong AWS account",
    "The role points to another account.",
    "Red, nothing touched.",
    g=(5, "The caller account differs from AWS_ACCOUNT_ID."),
)
ds(
    "x-owner",
    "Owner cannot be read",
    "describe-stacks fails.",
    "Red (fails closed), nothing touched.",
    "destroy",
    N,
    g=(6, "The guard exits 4 and no owner is written."),
)
ds(
    "x-unin",
    "Uninstall build fails",
    "The cluster does not answer or a Helm release cannot be removed.",
    "Red. No stack deleted; the NAT may have been recreated. Run again after fixing.",
    "destroy",
    N,
    r=(1, "helm_uninstall.sh fails before any stack is deleted."),
)
ds(
    "x-lb",
    "Load balancer does not go away",
    "The controller leaves a load balancer, target group or security group after 20 minutes.",
    "Red. No stack deleted; remove them by hand and run again.",
    "destroy",
    N,
    r=(1, "The wait times out: the network stack could not be deleted."),
)
ds(
    "x-del",
    "A stack ends in DELETE_FAILED",
    "CloudFormation cannot delete a stack, or a bucket cannot be emptied.",
    "Red. The stacks not yet reached stay; run again after fixing. The leftover check does not run.",
    "destroy",
    N,
    r=(1, "The failed events are printed and the loop stops."),
)
ds(
    "x-left",
    "Leftovers found",
    "Everything was deleted, but tagged or named resources remain after 4 passes.",
    "Red; the list is in the summary. Keys and secrets pending deletion do not count.",
    "destroy",
    N,
    r=(3, "post_destroy_check.py exits 1 (or 2 when a read fails)."),
)


def kn(
    id: str, title: str, desc: str, res: str, keep: str = "true", f: int | None = None, note: str | None = None
) -> None:
    a: dict[str, Any] = {"id": "k", "stage": "k_nat", "lane": 0, "via": "keep-nat.yml", "note": note}
    if f is not None:
        a.update(outcome="failed", failAt=f)
    add(
        id,
        "Keep NAT",
        title,
        desc,
        res,
        "manual",
        "Run Keep NAT",
        "any branch",
        ["keep = " + keep],
        "keep-nat.yml",
        [a],
    )


kn(
    "k-on",
    "Keep the NAT up",
    "keep = true (default) before 22:00 Panama.",
    "The 22:00 shutdown keeps the NAT. Nothing resets it: run again with false; it costs money meanwhile.",
    note="The parameter becomes true.",
)
kn(
    "k-off",
    "Back to the normal schedule",
    "keep = false.",
    "The next 22:00 shutdown removes the NAT.",
    "false",
    note="The parameter becomes false.",
)
kn(
    "k-none",
    "dev is not deployed",
    "The scheduler stack is not deployed, so the parameter does not exist. The workflow does not check this.",
    "Green, but put-parameter creates the parameter outside CloudFormation; the next deployment of the scheduler stack, which defines the same name, would likely fail until it is deleted by hand (deduced from the code, not run).",
    note="put-parameter --overwrite creates the missing parameter.",
)
kn(
    "k-read",
    "Write is refused",
    "put-parameter fails, for example for a missing permission.",
    "Red, nothing written.",
    f=3,
    note="put-parameter fails; the step stops.",
)
kn(
    "k-conf",
    "Missing configuration",
    "AWS_REGION or a secret is missing in the dev environment.",
    "Red before AWS.",
    f=0,
    note="The missing names are printed.",
)
kn(
    "k-acct",
    "Wrong AWS account",
    "The role points to another account.",
    "Red, nothing written.",
    f=2,
    note="The caller account differs from AWS_ACCOUNT_ID.",
)
DATA: dict[str, Any] = {
    "title": "EKS version: GitHub Actions pipelines",
    "subtitle": "Six workflows: CI, Deploy DEV, Deploy PROD, the reusable deployment, Destroy DEV and Keep NAT",
    "asOf": "2026-11-28",
    "source": ".github/workflows/*.yml and scripts/ (working tree, develop)",
    "files": files,
    "stages": stages,
    "scenarios": sc,
}
