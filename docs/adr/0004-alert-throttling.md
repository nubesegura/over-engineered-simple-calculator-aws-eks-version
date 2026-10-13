# ADR 0004: Throttle error alerts per error type

Status: Accepted (2026-10-13; amends requirement 5.1 of spec 001-create-baseline)

## Context

Requirement 5.1 asked for one notification per unexpected error. During a database outage every request fails with the same error, so the alerts topic would send one email per failed request, with identical content, and bury the owner's inbox.

## Decision (owner)

A `ThrottledErrorNotifier` wraps the SNS notifier: at most one alert per error type per interval, per process. The first alert of each type is always sent. Alerts of a type that arrive inside the interval are counted, and the count is added to the message of the next alert of that type ("N similar alert(s) suppressed since the previous one"). The interval is `ALERT_THROTTLE_SECONDS`, 300 by default, 0 to 86400; 0 sends every alert. The ingestion worker uses the same wrapper.

## Consequences

- The product wording is now: one alert per error type per interval instead of one per error ([functional.md](../functional.md)).
- The throttle is per process: with several replicas, each sends its own first alert. The error type is the exception class name, so different failures are still reported separately.
- The state is in memory; a restarted pod starts counting again.
- The count of suppressed alerts keeps the size of an outage visible.
