# ADR 0001: Trust the Amazon root certificates for the RDS Proxy

Status: Accepted (2026-10-13)

## Context

The workloads verify the database certificate (`sslmode=verify-full`). The first trust file held only the RDS global certificate bundle. In `dev`, every request that touched the database failed after about 10 seconds with `certificate verify failed`, while the load balancer kept every target `healthy`, because `/health` does not use the database.

RDS Proxy does not present an RDS certificate. It presents a certificate issued through AWS Certificate Manager, which chains to the Amazon Trust Services roots. A direct connection to the instance, used by the migration job and the rotation function, presents an RDS certificate.

## Decision

The image carries one trust file: the RDS global bundle followed by Amazon Root CA 1 to 4. The roots are downloaded over verified HTTPS during the image build and each one must match a SHA-256 value pinned in `src/rotation/package_bundle.py`; the whole file is validated before it is written. Verification stays on (`verify-full`) everywhere. The rotation function keeps the RDS bundle only, because it connects to the instance.

## Consequences

- Services, the ingestion worker and the migration job use the same file (`/app/db-trust-bundle.pem`, setting `SSL_ROOT_CERT`).
- A new Amazon root or a rotated RDS bundle needs an image rebuild; a changed root fails the build instead of being trusted.
- Design rule for later work: for each client of a database, state which endpoint it uses and which authority signs its certificate.
- The health check cannot detect this kind of failure. A real write through the proxy must be part of the acceptance after a first deployment (backlog: automatic smoke test).
