# Security policy

## Supported version

Only the latest commit on `main` is supported until the first tagged v2 release.

## Scope and secrets

The program is read-only. It does not accept exchange API keys and has no order,
withdrawal, margin, futures, perpetual, or leverage code path. Notification
credentials must be stored as GitHub Actions secrets or environment variables;
never commit them to the repository.

Report vulnerabilities privately through GitHub Security Advisories. Do not put
tokens, credentials, or personally identifying data in a public issue.
