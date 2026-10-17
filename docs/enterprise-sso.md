# Tenant OIDC sign-in (early enterprise SSO)

The API supports a tenant-configured OpenID Connect Authorization Code flow
with PKCE. This is an early integration, not a complete enterprise identity or
compliance product.

## Prerequisites

- The organization must be on the `enterprise` plan.
- Redis must be persistent and available to all API workers.
- Configure `WCP_PUBLIC_BASE_URL` to the externally reachable HTTPS origin,
  e.g. `https://console.example.com` (origin only; no path, query or fragment).
- Generate and store `WCP_SSO_ENCRYPTION_KEY` in the deployment secret manager:

  ```sh
  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```

  This key encrypts the OIDC client secret at rest. Keep it stable across API
  replicas and protect/rotate it with a documented secret migration; losing it
  makes the saved client secret unreadable.

- Register this exact callback URI with the identity provider:
  `https://console.example.com/api/auth/oidc/callback`.

## Configure a tenant

An organization owner/admin obtains a JWT from the normal sign-in flow and
calls `PUT /api/v1/sso/oidc` with that Bearer token:

```json
{
  "issuer": "https://idp.example.com/tenant",
  "client_id": "web-intelligence-console",
  "client_secret": "provided-by-the-idp",
  "allowed_domains": ["example.com"],
  "jit_provisioning": false,
  "default_role": "viewer"
}
```

The config response never returns the client secret. The `login_path` in the
response can be shared with organization members. The console also presents an
SSO sign-in option at `/login` using the 10-character organization ID.

Exact email domains only are supported; list each subdomain separately. Existing
accounts can link to an identity only when the IdP asserts a verified,
allowlisted email in the same organization. When JIT is enabled, new users are
created only inside that configured organization and only as `viewer` or
`member`; never as an admin or owner. JIT is disabled by default. Admins can
read a bounded SSO audit history at `GET /api/v1/sso/audit` and disable the
provider with `DELETE /api/v1/sso/oidc`.

## Protocol/security boundaries

- Uses state bound to an HttpOnly cookie, one-time state storage, nonce,
  PKCE-S256, HTTPS, and a five-minute login transaction.
- ID tokens are verified against cached JWKS and checked for RS256 signature,
  issuer, audience, expiration, nonce, verified email and the configured domain.
- OIDC discovery, authorization, token and JWKS endpoints must share the
  configured issuer hostname. Endpoints are HTTPS-only, redirects are disabled,
  and DNS resolution to non-public addresses is rejected. This deliberately
  excludes some providers that use separate endpoint hostnames.
- The client secret is encrypted with Fernet; login, client secrets, tokens and
  email addresses are not written to the SSO audit log. Audit events are capped
  at 1,000 per tenant and expire after 365 days.
- Use a trusted TLS-terminating reverse proxy and restrict who can configure
  tenant providers. Configure a stable public base URL; forwarded Host headers
  are not used to construct callback URLs.

This implementation does not provide SAML, SCIM, automated deprovisioning,
MFA policy enforcement, identity-provider initiated login, DNS-pinning against
all rebinding scenarios, a broad compliance audit system, or SOC 2/ISO
certification. Live integration against a production IdP and multi-replica
load testing remain deployment validation tasks.
