# One-time YouTube OAuth setup

1. In Google Cloud Console, select or create a project.
2. Enable **YouTube Data API v3** for that project.
3. Configure the OAuth consent screen. For an External app in Testing, add the publishing Google account as a test user.
4. Create **OAuth client ID → Desktop app** and download the JSON.
5. Run:

```bash
.venv/bin/python scripts/youtube_publish.py auth --client-secrets /absolute/desktop-client.json --profile main
```

6. Complete the Google consent page in the browser, then verify the selected channel:

```bash
.venv/bin/python scripts/youtube_publish.py preflight --profile main
```

The existing YouTube browser session helps select the account, but it does not grant API access by itself. The local authorization flow returns a refresh token so routine uploads do not require repeated browser consent. Google can still require reauthorization after revocation, security changes, invalidation, or testing-mode token expiry.

Do not use a Web application client whose redirect URI belongs to another site. Do not commit the downloaded client JSON or copy its secret into manifests, prompts, logs, or source code.
