# Security policy

## Supported version

Only the latest commit on `main` is supported until the first tagged v2 release.

## Scope and secrets

The program is read-only. It does not accept exchange API keys and has no order,
withdrawal, margin, futures, perpetual, or leverage code path. Notification
credentials and the optional `OPENAI_API_KEY` must be stored as GitHub Actions
secrets or environment variables; never commit them to the repository. The model
review receives only bounded public derived data and cannot call tools or alter the
effective recommendation. Provider errors and bodies are never written to reports.
Evidence grades are checked against allowlisted domains, and a process lock prevents
overlapping monitor runs from racing through delivery.

Report vulnerabilities privately through GitHub Security Advisories. Do not put
tokens, credentials, or personally identifying data in a public issue.
