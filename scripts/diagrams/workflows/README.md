# Workflow pipeline page generator

Sources of `docs/workflows-flow-animation.html`, the offline page that shows every GitHub Actions workflow of this
repository as release pipelines (jobs, steps and every scenario the YAML supports: pull request, push, manual deploy,
destroy, failures).

| File | What it holds |
|---|---|
| `stages.py` | The workflow files (role, triggers) and the stages: one stage is one job, or a part of a long job, with its tasks. Each task names the YAML steps it covers. |
| `scenarios.py` | The scenarios: trigger, which stages run, in which order (`after`), and how each ends (`outcome`, `failAt`, `skip`). |
| `engine.html` | The page engine (layout, playback, themes). Vendored as is; the data is injected, the engine is never edited by hand. |
| `build.py` | Checks the data against the real `.github/workflows/*.yml` and writes the page with the YAML line of every step. |

## Regenerate

From the repository root, with the tools of `scripts/requirements-dev.txt` installed (PyYAML is the only dependency):

```bash
python scripts/diagrams/workflows/build.py          # writes docs/workflows-flow-animation.html
python scripts/diagrams/workflows/build.py --check  # exit 1 if the committed page differs from what the sources give
```

The output is deterministic: the same sources give the same bytes. The build fails (and writes nothing) when a stage
names a job or step that does not exist, when a YAML step is not covered by any task, or when a scenario is
inconsistent; the messages say what to change.

## When to update

After any change in `.github/workflows/` or in a script those workflows call (`scripts/*.py`, `scripts/*.sh`):

1. Edit `stages.py` (tasks and step names, in execution order) and `scenarios.py` (new or changed scenarios). Read what
   the called scripts really do before writing a task description.
2. Set `asOf` in `scenarios.py` to the date of the change.
3. Run the build, open the page in a browser and look at the changed scenarios, then commit the sources and the page
   together.

A change that only moves YAML lines still changes the page (line numbers): run the build again.

## Rules for the data

- Plain English, no account ids, e-mail addresses, secret values or personal names (the build rejects 12-digit numbers
  and e-mail addresses).
- Stage name at most 28 characters, task label at most 34, at most 9 tasks per stage (split a long job in parts).
- `after` follows the real `needs`; parallel stages use different lanes.
- `skip` lists the 0-based tasks whose `if` is false in that scenario; tasks after `failAt` are shown as skipped unless
  the task has `always`.
- Draw a GitHub environment approval only where the YAML or the documentation says one exists or may exist.
