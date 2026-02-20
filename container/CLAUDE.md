# Sandbox Environment

You are running inside a sandboxed container. All your work must go through git.

## Git Remotes

You have two remotes:
- **`origin`** — your fork on Gitea (read-write). This is your workspace. You own it.
- **`upstream`** — the mirror of the real GitHub repo (read-only). The maintainer's source of truth.

Do not add other remotes.

## Git Workflow

### Syncing main

You must sync `main` from **both** remotes before starting new work. The maintainer
may have merged PRs on your fork (`origin`) or pushed changes to GitHub (`upstream`).

```bash
git checkout main
git pull origin main              # Get PRs the maintainer merged on your fork
git fetch upstream
git merge upstream/main           # Get changes from the real GitHub repo
git push origin main              # Keep your fork's main up to date
```

If `upstream/main` conflicts with `origin/main`: **upstream wins** — it is the real
repo. See the conflict resolution section in `~/repo-watch-prompt.md` for details.

### Branches

- **Use `agent/` prefix** for feature branches: `agent/add-auth`, `agent/fix-parser`.
- **Do not create branches without the `agent/` prefix** (except `main`).
- **Commit often locally.** Small commits with clear messages.
- **Push when you finish a logical chunk of work.** Not after every commit. Push when:
  - You've completed the task or a meaningful milestone
  - You're about to start a risky operation and want a remote backup
  - You're done for now and want the maintainer to be able to review
- The maintainer will squash-merge your branch, so individual commit count doesn't
  matter to them. What matters is that the final diff is clean and correct.

```bash
# Correct workflow
git checkout main
git pull origin main
git fetch upstream && git merge upstream/main
git push origin main
git checkout -b agent/my-feature
# ... work, committing as you go ...
git add -A && git commit -m "Add JWT validation middleware"
git add -A && git commit -m "Add tests for JWT validation"
# ... push when the feature is ready for review ...
git push origin agent/my-feature
```

## Verification

- Run tests before pushing. If tests exist, run them.
- If you add a feature, add or update tests for it.
- Run linters/formatters if configured in the project.
- Do not push code you haven't verified.

## What You Have Access To

- This workspace (the cloned repo)
- Internet access for API calls and package installation
- Git push/pull to Gitea (`origin` for push, `upstream` for fetch)

## What You Do NOT Have Access To

- The user's real GitHub repo (no credentials for it)
- The host filesystem (this is an isolated container)
- The local network (LAN access is blocked)

## Working Style

- Read existing code before making changes. Understand patterns before modifying.
- Prefer editing existing files over creating new ones.
- Keep changes focused. One branch per task.
- If unsure about an approach, create the branch, push what you have, and note
  the uncertainty in the commit message. The user will review.

## Gitea API

Your Gitea API token is in `$GITEA_TOKEN`. Base URL: `$GITEA_URL/api/v1`.
Repo path: `$GITEA_USER/$REPO_NAME`.

You can interact with issues and pull requests via curl. See `~/repo-watch-prompt.md`
for API examples and workflow guidelines.
