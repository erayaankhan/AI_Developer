"""dt command modules. dev_ticket.py stays the entry point and re-exports their names.

Patching rule for tests: a name a test patches (PROJECT_ROOT, DB_REPO, git, load_state, ...) is called through its
module (`common.git(...)`, `ticket_fetch.cmd_fetch(...)`) so one patch target covers every caller; patch it on the module
that defines it. Other names are imported bare.
"""
