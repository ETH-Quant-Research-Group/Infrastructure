# Installation Guide

## Prerequisites

- **Python 3.11** (see `.python-version`)
- **Git**
- **[uv](https://docs.astral.sh/uv/)**
 — used for all dependency and environment management

Install `uv` if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 1. Clone the Repository

```bash
git clone <repo-url>
cd infrastructure
```

---

## 2. Set Up the Virtual Environment

```bash
uv venv        # creates .venv using Python 3.11
uv sync        # installs all dependencies (including dev) from uv.lock
```

Alternatively, activate the environment manually for subsequent commands:

```bash
source .venv/bin/activate
```

---

## 3. Set Up Pre-commit Hooks

The repo uses `pre-commit` to enforce code quality checks on every commit 
(linting, formatting, type checking, secret scanning, etc.).

```bash
uv run pre-commit install
```

To run all hooks manually against the full codebase:

```bash
uv run pre-commit run --all-files
```

> **Note:** The `pip-audit` CVE scan hook only runs on `git push`, 
not on every commit — this is intentional as it is slower.

---

## 4. Initialise the Secrets Baseline

A `.secrets.baseline` file is already committed to the repo and 
used by `detect-secrets` to track known non-sensitive strings. 
If you add new files and want to update the baseline:

```bash
uv run detect-secrets scan > .secrets.baseline
```

---

## 5. Verify the Setup

Run the test suite to confirm everything is working:

```bash
uv run pytest
```

Coverage is enforced at a minimum of **75%**. 
Results are printed to the terminal alongside any missing lines.

---

## 6. Running Scripts

```bash
uv run script.py          # without activating the venv
# or
python script.py          # with the venv activated
```

---

## Managing Dependencies

```bash
uv add <package>          # add a runtime dependency
uv add --dev <package>    # add a dev-only dependency
uv remove <package>       # remove a dependency
```

After any dependency change, `uv.lock` is updated automatically 
— commit it alongside your changes.
