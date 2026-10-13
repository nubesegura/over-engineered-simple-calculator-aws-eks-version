# ADR 0011: Alarm and schedule names deviate from the acronym table

Status: Accepted (2026-10-13, owner decision; the names were already deployed on 2026-10-08)

## Context

The team naming table gives CloudWatch alarms the acronym `alrm` and EventBridge Scheduler schedules `evs`. This project named them `alm-` and `sch-`. The alarm name and the schedule name are immutable: renaming forces a replacement, which the change set guard stops and which needs a one-off run that allows replacements. The review of 2026-10-13 counted 19 alarms and 3 schedules affected.

## Decision

The names stay. This is a recorded exception to the table, not a precedent:

- Alarms: 17 in `alarms.yaml`, one in `rotation.yaml` (database rotation errors), one in `scheduler.yaml` (NAT scheduler errors). Schedules: `nat-down` and `nat-up` in `scheduler.yaml`, `reminder` in `alarms.yaml`.
- If the environment is destroyed and created again, these resources should be created as `alrm-` and `evs-` from the start, changing the names in the templates and in the tests that read them in the same change.
- The session document `ssmd-` of the bastion and the log group `/aws/ssm/session/...` (a prefix fixed by AWS) are separate exceptions, not decided here.

## Consequences

- No replacement of alarms in `dev`, no gap in alerting.
- A reader of the console sees two prefixes that differ from the team table; this ADR and `templates/README.md` explain why.
