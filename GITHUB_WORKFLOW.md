# GitHub Workflow

Practical git reference for working on this repo. Production is served from `main` via GitHub Pages — every push to `main` triggers a redeploy, so `main` stays deploy-ready and work happens on feature branches reviewed through Pull Requests.

---

## 1. Start a new branch

Before branching, make sure your local `main` matches the remote.

```bash
git checkout main
git pull origin main
git status          # should be "working tree clean"
```

Then create and switch to a feature branch. Use `feature/`, `fix/`, or `chore/` as a prefix and a short kebab-case description.

```bash
git checkout -b feature/layout-redesign
```

Good names: `feature/theme-explorer-polish`, `fix/kpi-overflow`, `chore/tailwind-upgrade`.
Bad names: `test`, `new-stuff`, `andrea-branch`.

---

## 2. Work on the branch

Commit in small, logical chunks. One commit = one coherent change. Small commits make review and rollback easy.

```bash
git status                          # see what changed
git diff                            # review unstaged changes
git add calls/index.html calls/css  # stage specific files (prefer this over `git add .`)
git diff --staged                   # review what's about to be committed
git commit -m "Tighten KPI card spacing on mobile"
```

**Commit message style** — imperative mood, ≤72 chars for the subject line. If more context is needed, add a blank line then a body:

```
Refactor theme explorer breadcrumb

Pulls the breadcrumb out of theme-explorer.js so it can be reused
by the upcoming filter-history panel.
```

---

## 3. Push the branch to GitHub

First push sets the upstream with `-u` so future `git push` / `git pull` know where to go.

```bash
git push -u origin feature/layout-redesign
```

Subsequent pushes are just:

```bash
git push
```

---

## 4. Open a Pull Request

1. GitHub's response to the push prints a URL — open it. Or go to https://github.com/andrea-revibe/ziwo-dashboard and click **Compare & pull request**.
2. Title = what the PR does, in one line. Description = the *why*, screenshots for UI changes, and any testing notes.
3. Base = `main`. Compare = your feature branch.
4. Review the **Files changed** tab yourself before asking anyone else to — catching your own typos here saves a round trip.
5. Merge when ready. Prefer **Squash and merge** for feature branches so `main` history stays one-commit-per-PR.

---

## 5. After the PR merges

Clean up locally so stale branches don't pile up.

```bash
git checkout main
git pull origin main                # get the squashed merge commit
git branch -d feature/layout-redesign      # delete local branch
git push origin --delete feature/layout-redesign  # delete remote branch (if GitHub didn't already)
```

---

## 6. Common scenarios

### a) You committed to `main` by accident

You made a commit on `main` but it should've been on a feature branch. Move it.

```bash
git branch feature/layout-redesign   # create a branch pointing at current HEAD (keeps the commit)
git reset --hard origin/main         # rewind main to match remote — destroys local main changes
git checkout feature/layout-redesign # your commit is safe here
```

> `--hard` discards working-tree changes. Only run it when you're sure the commit is preserved on the new branch.

### b) Undo the last commit but keep the changes

You committed too early and want to edit the files more.

```bash
git reset --soft HEAD~1   # uncommits but keeps changes staged
# edit files...
git commit -m "Better message"
```

If you want to throw the changes away entirely: `git reset --hard HEAD~1` (destructive — use carefully).

### c) Amend the last commit (only before pushing)

Fix a typo in the last commit message, or add a forgotten file:

```bash
git add forgotten-file.js
git commit --amend -m "Updated message"
```

**Never amend a commit that's already pushed to a shared branch** — it rewrites history and will confuse anyone who pulled it.

### d) Merge conflict when pulling

`git pull` fails with a conflict:

```bash
git status                # lists conflicted files
# open each file — look for <<<<<<< ======= >>>>>>> markers
# edit to keep what you want, delete the markers
git add path/to/resolved-file.js
git commit                # finishes the merge
```

If you get in over your head: `git merge --abort` puts you back where you were before the pull.

### e) Sync your feature branch with latest `main`

Your branch has been open a while and `main` moved on. Bring those changes in:

```bash
git checkout feature/layout-redesign
git fetch origin
git merge origin/main         # creates a merge commit — simplest and safest
# (alternative: git rebase origin/main — cleaner history but rewrites your branch)
git push                      # push the updated branch
```

### f) Revert a commit that's already on `main`

The commit is already pushed and deployed — you can't just delete it. Create a new commit that undoes it.

```bash
git checkout main
git pull
git revert <commit-sha>       # opens editor for the revert commit message
git push
```

Find the sha with `git log --oneline -n 10`.

### g) Discard local changes to a specific file

You edited a file, hate what you did, want the version on `main` back.

```bash
git checkout -- calls/index.html     # discard unstaged changes
# or, if already staged:
git restore --staged calls/index.html
git checkout -- calls/index.html
```

### h) You're on the wrong branch and have uncommitted work

Move the work to the right branch without losing it:

```bash
git stash                            # saves changes, leaves working tree clean
git checkout feature/correct-branch
git stash pop                        # reapplies the stashed changes
```

---

## Quick reference

| Task | Command |
|---|---|
| See what changed | `git status` / `git diff` |
| Recent history | `git log --oneline -n 10` |
| Current branch | `git branch --show-current` |
| List all branches | `git branch -a` |
| Switch branches | `git checkout <name>` |
| New branch from current | `git checkout -b <name>` |
| Stage specific files | `git add path/to/file` |
| Commit | `git commit -m "message"` |
| Push new branch | `git push -u origin <name>` |
| Push existing branch | `git push` |
| Update local `main` | `git checkout main && git pull` |
| Delete local branch | `git branch -d <name>` |
| Delete remote branch | `git push origin --delete <name>` |

---

## Deploy note

Because GitHub Pages rebuilds on every push to `main`, merging a PR is the deploy step. There's nothing else to run. If a merge breaks production, the fastest fix is usually `git revert` (scenario f) — it's one command and keeps history honest.
