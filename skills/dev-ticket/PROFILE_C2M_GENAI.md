# C2M CI profile — the `C2M - GenAI` repo

Read with `PROFILE_C2M.md` (shared by every C2M CI repo) when `dt config-check` reports `"repo_key": "genai"`.
Where they differ, this file wins.

## Tickets

- `Node = GenAI`, tag **`GENAI`**, parent: an active GenAI release Epic (e.g. "GenAI release 1.22.0 stories").
- Milestones are named `GenAI 1.x.y` (e.g. GenAI 1.23.0 and the open patch milestones).
- Code_Merge has skipped GenAI tickets by its own rules before (#18955 in GenAI 1.21.2 sat in DevOps Staging unpicked):
  `NOT_PICKED` → ask DevOps.

## Gates

- **Python** (GenAIService, EmbeddingService, connector-bridge, tests): `py-compile` for every changed `.py`, and
  `pytest -q <changed test files>` from the nearest folder with `pyproject.toml` / `pytest.ini`, using that service's
  `.venv` (or `venv`). No environment → the gate is `SKIPPED` with the reason; tell the developer how to create it
  (`python3 -m venv .venv` in the service, then its `install_packages.sh` / `pip install -e .`), never install packages
  yourself without a yes.
- **Maven**: `mvn -q -B test` in each changed module of `gen-ai-designer-service` (`maven-test:<module>`).
- **Angular** (`ng test`): `gen-ai-designer-mfe`, `prompt-designer`.
- No pre-commit config in this repo: that gate does not run. `--skip <gate>` only with the developer's yes and a reason
  in SUMMARY.md.

## Functional checks (step 7d)

The two Angular apps are module-federation remotes. Serve the one you changed on its own (`ng serve` in
`gen-ai-designer-mfe` or `prompt-designer`, `http://localhost:4200`) and check it in Chrome; the developer signs in. For
a Python or Java service change, check the endpoint locally (curl / the service's README start command) when its
environment exists. If that is not practical, `dt func-mode {N} skip --reason "…" --confirmed` on the developer's yes.
