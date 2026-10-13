# ADR 0012: Library log levels are capped and credentials are redacted

Status: Accepted (2026-10-13)

## Context

A temporary `LOG_LEVEL=DEBUG` in the `dev` values made `botocore` print request details, including temporary AWS credentials, to the pod log. Pod logs are read from the bastion and are not shipped to CloudWatch, but a credential in any log is a defect.

## Decision

- The application keeps the loggers of `botocore`, `boto3`, `urllib3`, `s3transfer` and `psycopg` at WARNING whatever `LOG_LEVEL` says. `LOG_LEVEL` sets only the application's own loggers.
- A logging filter scrubs credential-looking values (authorization and security token headers, access key and session token assignments, SigV4 credential strings) from every record before it is written.
- Exceptions are logged by type, cause type and stack frames only; driver and SDK messages are never logged because they may carry request details.
- `LOG_LEVEL` is `INFO` in both environments; DEBUG is an explicit, temporary override (`--set-string app.logLevel=DEBUG`).
- Every pod and the migration job receive `ENVIRONMENT`, which appears in each log record and alert.

## Consequences

- Debugging the AWS calls of a pod needs a different tool than `LOG_LEVEL`.
- The redaction covers AWS credential patterns only; it is a last line of defence, not a license to log connection strings or passwords (backlog: widen the pattern).
