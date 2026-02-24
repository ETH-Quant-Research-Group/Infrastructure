# Infrastructure
General Fund Infrastructure with most important features.



### UV

This project uses [uv](https://docs.astral.sh/uv/) for dependency management. No other package manager is needed.

**Install uv:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Set up the project:**
```bash
uv venv          # create virtual environment
uv sync          # install dependencies
```

**Run scripts:**

To run python scripts, you can either use the following:
```bash
uv run script.py
```
or more conventionally:
```bash
source .venv/bin/activate
python script.py
```

**Add/remove dependencies:**
```bash
uv add <package>
uv remove <package>
```
