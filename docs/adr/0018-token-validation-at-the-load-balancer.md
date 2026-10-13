# ADR 0018: Token validation at the load balancer, with a separate preflight Ingress

Status: Accepted (2026-10-08)

## Context

The `sls` and `ecs` versions authenticate at their edge. The pods should not carry token-verification code or hold the pool keys, and a request without a valid token must not reach a pod. Browsers send the CORS preflight without a token.

## Decision

The Application Load Balancer verifies the Cognito ID token (`jwt-validation` annotation: JWKS endpoint of the pool, issuer, `aud` equal to the app client, `token_use` equal to `id`, signature and expiry). Two Ingress objects share one load balancer (one group):

- `calculator-preflight` (group order 10): rules for the method `OPTIONS` only, no authentication.
- `calculator-api` (group order 20): the same paths with `jwt-validation` for every other method.

Paths use `Exact` matching; `/health` has no route, so it is not reachable through the load balancer (the target group health check calls it). The listener default is a fixed 404 built by the controller. HTTP on 80 redirects to 443; the policy is `ELBSecurityPolicy-TLS13-1-2-2021-06`. The ALB security group allows egress 443 to reach the JWKS endpoint. The pods decode the `sub` claim for the access log only and never verify the token.

## Consequences

- A rejected token gets the load balancer's own 401, which cannot be customised and is not the JSON error shape.
- Each Ingress gets its own target group per service (two per service, 20 listener rules in total).
- Any principal that can create Ingress objects with the same group name can add rules to the load balancer; Kubernetes RBAC must stay restricted (only the deployment role creates them).
- The pods trust the network: ADR 0003 limits who may reach them.
