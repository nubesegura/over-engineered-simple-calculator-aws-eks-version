"""Builds docs/workflows-flow-animation.html from stages.py, scenarios.py and engine.html.

Usage (from the repository root):
    python scripts/diagrams/workflows/build.py            write the page
    python scripts/diagrams/workflows/build.py --check    exit 1 when the committed page is not what the sources give
    python scripts/diagrams/workflows/build.py --out FILE write the page somewhere else

Before writing, the data is checked against the real .github/workflows files: every job and step named by a stage
must exist, every YAML step must be drawn, and the scenarios must be consistent. Each task gets the YAML line numbers
of its steps. Needs PyYAML (scripts/requirements-dev.txt).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from scenarios import DATA

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PAGE = REPO / "docs" / "workflows-flow-animation.html"
OUTCOMES = ("succeeded", "failed", "skipped", "rejected", "canceled")
PRIVACY = ((r"(?<!\d)\d{12}(?!\d)", "12-digit number"), (r"[\w.+-]+@[\w-]+\.\w{2,}", "email"))


def get(node: Any, key: str) -> Any:
    for k, v in getattr(node, "value", []):
        if k.value == key:
            return v
    return None


def read_workflows(repo: Path) -> tuple[dict[tuple[str, str], dict[str, int]], dict[str, list[str]]]:
    """real[(file, job)] = {step key: line}; calls[file] = reusable workflows called by the file."""
    real: dict[tuple[str, str], dict[str, int]] = {}
    calls: dict[str, list[str]] = {}
    for f in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        root = yaml.compose(f.read_text(encoding="utf-8"))
        calls[f.name] = []
        for jk, jv in get(root, "jobs").value:
            use = get(jv, "uses")
            if use is not None:
                calls[f.name].append(use.value.split("/")[-1])
                continue
            steps: dict[str, int] = {}
            for n, st in enumerate(get(jv, "steps").value):
                nm, us = get(st, "name"), get(st, "uses")
                key = nm.value if nm else (us.value.split("@")[0] if us else f"#{n + 1}")
                if key in steps:
                    key = f"{key} #{n + 1}"
                steps[key] = st.start_mark.line + 1
            real[(f.name, jk.value)] = steps
    return real, calls


def check_stages(d: dict[str, Any], real: dict[tuple[str, str], dict[str, int]], err: list[str]) -> None:
    covered: dict[tuple[str, str], set[str]] = {}
    for sid, s in d["stages"].items():
        key = (s["file"], s["job"])
        if key not in real:
            err.append(f"stage {sid}: job {key} not in YAML")
            continue
        if not s.get("summary"):
            err.append(f"stage {sid}: summary missing")
        if len(s["name"]) > 28:
            err.append(f"stage {sid}: name over 28 chars")
        if len(s["tasks"]) > 9:
            err.append(f"stage {sid}: {len(s['tasks'])} tasks; split into two stages of the same job (max 9)")
        for t in s["tasks"]:
            if len(t["t"]) > 34:
                err.append(f"stage {sid}: task label over 34 chars: {t['t']}")
            if not t.get("do"):
                err.append(f"stage {sid}: task '{t['t']}' needs 'do'")
            t["lines"] = []
            for st in t["steps"]:
                if st not in real[key]:
                    err.append(f"stage {sid}: step '{st}' not in {key}; available: {list(real[key])}")
                else:
                    t["lines"].append(real[key][st])
                    covered.setdefault(key, set()).add(st)
    for key, steps in real.items():
        miss = set(steps) - covered.get(key, set())
        if miss:
            err.append(f"{key}: YAML steps not drawn: {sorted(miss)}")


def column(by: dict[str, Any], i: dict[str, Any]) -> int:
    return 1 + max(column(by, by[a]) for a in i["after"]) if i.get("after") else 0


def check_scenario(d: dict[str, Any], sc: dict[str, Any], used: set[str], err: list[str]) -> None:
    n = sc.get("id", "?")
    for k in ("group", "title", "desc", "result", "trigger", "stages"):
        if not sc.get(k):
            err.append(f"scenario {n}: {k} missing")
    if sc["trigger"]["type"] not in ("push", "pr", "manual", "schedule", "call"):
        err.append(f"scenario {n}: bad trigger type")
    if sc["trigger"].get("entry") not in d["files"]:
        err.append(f"scenario {n}: trigger.entry must be a workflow file")
    by = {i["id"]: i for i in sc["stages"]}
    if len(by) != len(sc["stages"]):
        err.append(f"scenario {n}: duplicate instance ids")
    bad = [a for i in sc["stages"] for a in i.get("after", []) if a not in by]
    if bad:
        err.append(f"scenario {n}: unknown after {bad}")
        return
    cells: dict[tuple[int, int], str] = {}
    jobs: dict[tuple[str, str, str], list[tuple[tuple[int, int], str]]] = {}
    for i in sc["stages"]:
        if i["stage"] not in d["stages"]:
            err.append(f"scenario {n}: unknown stage {i['stage']}")
            continue
        used.add(i["stage"])
        s = d["stages"][i["stage"]]
        cell = (column(by, i), i.get("lane", 0))
        if cell in cells:
            err.append(f"scenario {n}: {i['id']} and {cells[cell]} share column {cell[0]} lane {cell[1]}; change lane")
        cells[cell] = i["id"]
        jobs.setdefault((s["file"], s["job"], i.get("run", "")), []).append((cell, i["id"]))
        o = i.get("outcome", "succeeded")
        if o not in OUTCOMES:
            err.append(f"scenario {n}: {i['id']} bad outcome {o}")
        if o == "failed" and not 0 <= i.get("failAt", -1) < len(s["tasks"]):
            err.append(f"scenario {n}: {i['id']} failed needs failAt (0-based task index)")
        if any(not 0 <= k < len(s["tasks"]) for k in i.get("skip", [])):
            err.append(f"scenario {n}: {i['id']} skip index out of range")
        blocked = any(by[a].get("outcome") in OUTCOMES[1:] for a in i.get("after", []))
        if blocked and o != "skipped" and not i.get("always"):
            err.append(f"scenario {n}: {i['id']} runs after a failed/skipped stage; set outcome skipped or always:true")
    for job, lst in jobs.items():
        if len(lst) > 1:
            xs, ys = [c[0] for c, _ in lst], [c[1] for c, _ in lst]
            for c, iid in cells.items():
                if min(xs) <= c[0] <= max(xs) and min(ys) <= c[1] <= max(ys) and iid not in [z for _, z in lst]:
                    err.append(f"scenario {n}: {iid} sits inside the frame of job {job}; change lane")


def render(repo: Path) -> tuple[bytes, list[str], str]:
    """Returns the page, the list of errors and a one-line summary."""
    d: dict[str, Any] = json.loads(json.dumps(DATA))  # a private copy: the checks add line numbers to it
    err: list[str] = []
    real, calls = read_workflows(repo)
    if set(d["files"]) != set(calls):
        err.append(f"files differ: data-only {set(d['files']) - set(calls)}, yaml-only {set(calls) - set(d['files'])}")
    for f, x in d["files"].items():
        x["calls"] = list(dict.fromkeys(calls.get(f, [])))
        if not x.get("role") or not x.get("triggers"):
            err.append(f"{f}: role and triggers required")
    check_stages(d, real, err)
    used: set[str] = set()
    for sc in d["scenarios"]:
        check_scenario(d, sc, used, err)
    unused = set(d["stages"]) - used
    if unused:
        err.append(f"stages never used in a scenario: {sorted(unused)}")
    txt = json.dumps(d, ensure_ascii=False, indent=1)
    for pattern, what in PRIVACY:
        found = re.search(pattern, txt)
        if found:
            err.append(f"privacy: {what} found: {found.group()}")
    html = (HERE / "engine.html").read_text(encoding="utf-8")
    html = html.replace("__TITLE__", d["title"]).replace("/*DATA*/null", txt.replace("</", "<\\/"))
    summary = (
        f"files={len(calls)} jobs={len(real)} steps={sum(map(len, real.values()))} "
        f"stages={len(d['stages'])} scenarios={len(d['scenarios'])}"
    )
    return html.replace("\r\n", "\n").encode("utf-8"), err, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=PAGE, help="where to write the page (default: %(default)s)")
    parser.add_argument("--check", action="store_true", help="compare with the committed page instead of writing")
    args = parser.parse_args(argv)
    page, err, summary = render(REPO)
    if err:
        print("\n".join("FAIL " + e for e in err))
        return 1
    if args.check:
        if PAGE.is_file() and PAGE.read_bytes() == page:
            print(f"OK {PAGE.relative_to(REPO)} is up to date ({summary})")
            return 0
        print(f"FAIL {PAGE.relative_to(REPO)} is stale: run python scripts/diagrams/workflows/build.py")
        return 1
    args.out.write_bytes(page)
    print(f"OK {args.out} {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
