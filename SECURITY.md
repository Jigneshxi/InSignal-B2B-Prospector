# Security notes

- The session lives in a persistent Chrome profile (default `.li-profile/`).
  That profile is a live credential: anyone with it can use your LinkedIn
  session. It is gitignored; never copy or commit it.
- The tool never handles your password. You sign in yourself, once, inside the
  seat's own Chrome window.
- Requests are sent with your session cookies attached by the profile itself or
  read only at runtime; no cookie values are logged, hardcoded, or written to
  disk anywhere else.
- Signal webhook addresses are read from the `LINKEDIN_SIGNAL_WEBHOOK_URL`
  environment variable. Do not put webhook addresses in a committed config file.
- Output CSVs, JSONL signal exports, and the default SQLite signal database are
  gitignored; treat them as local data and do not commit pulled lists.
- Report vulnerabilities by opening a private security advisory on GitHub.
