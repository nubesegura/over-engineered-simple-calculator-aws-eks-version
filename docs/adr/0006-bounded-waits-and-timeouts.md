# ADR 0006: Bounded waits for every dependency

Status: Accepted (2026-10-13)

## Context

Three problems shared one cause, a wait without a limit:

- The alert publish (a blocking boto3 call) ran on the event loop that also answers `/health`. With the NAT gateway absent or SNS slow, one failing request could freeze the loop, the probes would fail and Kubernetes would restart a healthy pod.
- boto3 defaults are 60 seconds to connect and 60 to read, with retries.
- A database outage made every request wait 10 seconds for a connection while `/health` stayed green by design (requirement 3.4), so worker threads piled up and the outage lasted longer.

## Decision

- Database: connect timeout and pool timeout of 3 seconds (`DB_CONNECT_TIMEOUT_SECONDS`, `DB_POOL_TIMEOUT_SECONDS`, 1 to 30), at most 20 requests waiting for a connection (`DB_POOL_MAX_WAITING`, 1 to 1000; more fail at once). The pool holds 1 to 5 connections per pod.
- AWS clients: connect 2 seconds, read 3 seconds, two attempts in standard mode. The queue client reads for up to 30 seconds because the long poll holds the connection for 20.
- The 500 path publishes the alert in a worker thread (`run_in_threadpool`), so the loop keeps serving `/health`.
- A request that cannot get a connection fails with the same generic 500 and `INTERNAL_ERROR` as any persistence error.

## Consequences

- During an outage the owner gets a fast error instead of a slow one, and pods keep answering probes.
- A slow but healthy database can now fail requests that waited more than 3 seconds; calculations and history pages take milliseconds when healthy.
- No circuit breaker (YAGNI).
