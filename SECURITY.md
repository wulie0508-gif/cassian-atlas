# Security policy

## Supported version

The latest `main` branch is supported while the project is pre-1.0.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when available. Do not include learner names, answers, scores, source documents, access tokens, or database copies in a public issue.

Describe:

- the affected version or commit;
- the smallest reproducible sequence;
- the expected privacy or integrity boundary;
- whether the issue could expose local files or bypass evidence verification.

## Security model

OpenTutor Ledger is designed for localhost use. It does not provide internet-facing authentication or authorization. Do not expose the local HTTP server to an untrusted network. Keep private runtime data outside the repository and protect it with normal operating-system access controls and backups.
