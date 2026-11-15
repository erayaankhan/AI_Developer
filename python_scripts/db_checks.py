#!/usr/bin/env python3
"""
DB repo checks for the dev-ticket workflow (D-51). Runs as a gate in step 7a when the
ticket has DB files (state.json "db": {"ticket": D, "files": [...]}).

The DB repo (C2M_CI_DB) has no tooling of its own, so these checks encode its conventions,
measured on origin/master 2026-09-29 (2,755 SP files, 140 functions):

  SP template (new files must match exactly; edited files need the essentials):
      /*!50003 DROP PROCEDURE IF EXISTS  `Name` */;

      DELIMITER $$

      /*!50003 CREATE PROCEDURE `Name`(
        IN vParam INT
      )
      BEGIN
        ...
      END */$$
      DELIMITER ;

  Function template (same shape; 100 of 140 functions use it):
      /*!50003 DROP FUNCTION IF EXISTS  `Name` */;

      DELIMITER $$

      /*!50003 CREATE FUNCTION `Name`(p INT) RETURNS INT
      BEGIN
        ...
      END */$$
      DELIMITER ;

  D-52: every SP/function the ticket touches, new OR modified, must be in the template.
  `--normalize FILE [--write]` converts a file: header/footer rewritten, body kept byte-for-byte
  except DEFINER removed and inner /* */ comments turned into -- comments. Measured on
  origin/master (3,048 files): 2,363 already in template (whitespace ignored), 610 convertible,
  75 need a manual fix.

Levels:
  ERROR  fails the gate          STOP  hard stop, developer must confirm (D-51 q3)
  WARN   goes into the review brief

Checks: placement, naming (object = file name; DDL_DML = {D}.sql / {D}_{n}.sql), SP/function
structure, no DEFINER / USE, destructive statements (STOP), re-runnability, dynamic SQL,
hygiene, and "every SP the WF diff calls exists" (RunSp*("Name", ...)).

    python db_checks.py --ticket 18474
    python db_checks.py --db-ticket 18533 --files Database/C2M/SP/X.sql   (ad hoc)

Ends with: RESULT: {"status": "PASS"|"FAIL"|"STOP", "errors": [...], "stops": [...], "warnings": [...]}
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_gates import BASE, CONFIG, PROJECT_ROOT, changed_files, guarded, ticket_arg  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

DB = CONFIG["db"]
DB_REPO = (Path(__file__).resolve().parent / DB["repo_path"]).resolve()
# Object folders come from config db.sql_folders (single source; SP, Functions, Tables, DDL_DML).
FOLDER_RE = re.compile(r"^Database/(?P<schema>[^/]+)/(?P<kind>" + "|".join(map(re.escape, DB["sql_folders"]))
                       + r")(/.*)?/(?P<stem>[^/]+)\.sql$")
# db.layout = "flat": scripts sit directly in Database/<schema>/ (one file may hold several objects, any name). Then the
# per-kind template and naming rules do not apply; destructive-statement, hygiene and re-runnability checks still do.
FLAT = DB.get("layout") == "flat"
if FLAT:
    FOLDER_RE = re.compile(r"^Database/(?P<schema>[^/]+)/(?P<kind>)(?P<stem>[^/]+)\.sql$")
# SP/function structure is defined once, by normalize_object() (D-52).

DEFINER = re.compile(r"\bDEFINER\s*=", re.I)
USE_DB = re.compile(r"^\s*USE\s+`?\w+`?\s*;", re.I | re.M)
DESTRUCTIVE = [
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.I), "DROP TABLE/DATABASE"),
    (re.compile(r"\bTRUNCATE\s+(TABLE\s+)?`?\w+", re.I), "TRUNCATE"),
    (re.compile(r"\bALTER\s+TABLE\b[^;]*?\bDROP\s+(COLUMN|INDEX|KEY|PRIMARY\s+KEY|FOREIGN\s+KEY|CONSTRAINT|CHECK)\b",
                re.I | re.S), "ALTER TABLE ... DROP COLUMN/INDEX/KEY"),
    (re.compile(r"\bDROP\s+INDEX\b", re.I), "DROP INDEX"),
]
# `DROP TABLE IF EXISTS tmp_x` inside an SP is housekeeping on a scratch table, not data loss: warn, don't stop.
SCRATCH_DROP = re.compile(r"\bDROP\s+TABLE\s+IF\s+EXISTS\s+`?(tmp|temp)\w*`?\s*$", re.I)
STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"")
REFERENTIAL = re.compile(r"\b(ON|FOR)\s+(UPDATE|DELETE)\b", re.I)  # ON UPDATE CURRENT_TIMESTAMP, FOR UPDATE, ON DELETE CASCADE
UPDATE_SET = re.compile(r"\bUPDATE\b[\s\S]*?\bSET\b", re.I)
DELETE_STMT = re.compile(r"\bDELETE\b", re.I)
WHERE_WORD = re.compile(r"\bWHERE\b", re.I)


def destructive_statements(code: str) -> list:
    """[(label, normalised statement)] for every destructive statement in `code` (comments already stripped).

    Works per statement (split on ';'), after blanking string literals and ON/FOR UPDATE/DELETE, so it covers
    qualified names, aliases, UPDATE ... JOIN, multi-table DELETE, ALTER ... DROP COLUMN, and ignores the word
    inside strings. UPDATE/DELETE need a WHERE somewhere in the same statement."""
    found = []
    for raw in code.split(";"):
        stmt = REFERENTIAL.sub(" ", STRING_LITERAL.sub("''", raw))
        norm = re.sub(r"\s+", " ", raw).strip().lower()
        if not norm:
            continue
        for rx, label in DESTRUCTIVE:
            if rx.search(stmt):
                if label == "DROP TABLE/DATABASE" and SCRATCH_DROP.search(stmt.strip()):
                    continue
                found.append((label, norm))
        if UPDATE_SET.search(stmt) and not WHERE_WORD.search(stmt):
            found.append(("UPDATE without WHERE", norm))
        if DELETE_STMT.search(stmt) and not WHERE_WORD.search(stmt):
            found.append(("DELETE without WHERE", norm))
    return found


def destructive_token(name: str, statements: list) -> str:
    """What the developer confirms: the file AND the exact destructive statements in it. A statement added to the
    file later changes the token, so an earlier yes can't cover it."""
    digest = hashlib.sha256("\n".join(sorted({s for _, s in statements})).encode("utf-8")).hexdigest()[:12]
    return f"{name}@{digest}"
CREATE_TABLE_NO_GUARD = re.compile(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", re.I)
INSERT_NO_GUARD = re.compile(r"\bINSERT\s+INTO\b[^;]*;", re.I | re.S)
ADD_COLUMN = re.compile(r"\bALTER\s+TABLE\b[^;]*\bADD\s+(COLUMN\s+)?`?\w+", re.I | re.S)
DYNAMIC_SQL = re.compile(r"\bPREPARE\s+\w+\s+FROM\b", re.I)
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
RUNSP_CALL = re.compile(r"RunSp\w*\(\s*\"(?P<name>[A-Za-z0-9_]+)\"")


def git(repo: Path, *args) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout if proc.returncode == 0 else ""


def strip_comments(sql: str) -> str:
    """Drop -- and /* */ comments, but keep MySQL /*!50003 ... */ bodies (they're executed)."""
    sql = re.sub(r"/\*(?!!)[\s\S]*?\*/", " ", sql)
    return re.sub(r"--[^\n]*", " ", sql)


# ---------------------------------------------------------------------- template normaliser (D-52)

CREATE_OBJ = re.compile(
    r"(/\*!50003\s+)?CREATE\s+(DEFINER\s*=\s*(`[^`]*`@`[^`]*`|'[^']*'@'[^']*'|\S+)\s+)?"
    r"(?P<kw>PROCEDURE|FUNCTION)\s+`?(?P<name>\w+)`?", re.I)
BODY_END = re.compile(r"\bEND\b\s*(\*/)?\s*\$\$", re.I)
LEADING_OK = re.compile(r"^\s*(--[^\n]*|DELIMITER\s+\S+|(/\*!50003\s+)?DROP\s+(PROCEDURE|FUNCTION)\s+IF\s+EXISTS\s+`?\w+`?\s*(\*/)?\s*(;|\$\$)?)?\s*$", re.I)
TRAILING_OK = re.compile(r"^\s*(DELIMITER\s*;|--[^\n]*)?\s*$", re.I)


def _convert_block_comments(body: str):
    """Turn /* ... */ comments inside a body into -- line comments, skipping string literals and
    existing -- comments. Returns (new_body, converted_count) or (None, reason) when it isn't safe."""
    out, i, n, count = [], 0, len(body), 0
    while i < n:
        ch = body[i]
        if ch in "'\"`":  # string / quoted identifier: copy verbatim (MySQL doubles the quote to escape)
            j = i + 1
            while j < n:
                if body[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if body[j] == ch:
                    if j + 1 < n and body[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            literal = body[i:j + 1]
            if "*/" in literal:
                return None, "a string literal contains '*/', which would end the /*!50003 wrapper"
            out.append(literal)
            i = j + 1
        elif body.startswith("--", i) or ch == "#":
            j = body.find("\n", i)
            j = n if j == -1 else j
            line = body[i:j]
            if "*/" in line:
                line = line.replace("*/", "* /")  # would end the wrapper even inside a -- comment
            out.append(line)
            i = j
        elif body.startswith("/*!", i):
            return None, "the body contains a /*!NNNNN ... */ versioned comment; can't nest it in the wrapper"
        elif body.startswith("/*", i):
            j = body.find("*/", i + 2)
            if j == -1:
                return None, "unterminated /* comment in the body"
            text = body[i + 2:j].strip("\n")
            line_start = body.rfind("\n", 0, i) + 1
            indent = re.match(r"[ \t]*", body[line_start:i]).group(0)  # keep the comment's column
            lines = [("-- " + l.strip()) if l.strip() else "--" for l in (text.splitlines() or [""])]
            out.append(("\n" + indent).join(lines))
            count += 1
            i = j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out), count


def normalize_object(text: str, kind: str, stem: str) -> dict:
    """Rewrite an SP/function file into the defined template (D-52), keeping the body.

    Returns {"status": "ALREADY_TEMPLATE"|"NORMALIZED"|"NEEDS_MANUAL", "text": ..., "notes": [...]}.
    Leading `--` comment lines at the top of the file are kept. Refuses (NEEDS_MANUAL) rather than
    guessing when the file has anything unusual.
    """
    src = text.replace("\r\n", "\n")
    kw_expected = "PROCEDURE" if kind == "SP" else "FUNCTION"
    creates = list(CREATE_OBJ.finditer(src))
    if len(creates) != 1:
        return {"status": "NEEDS_MANUAL", "notes": [f"expected exactly one CREATE {kw_expected}, found {len(creates)}"]}
    m = creates[0]
    if m.group("kw").upper() != kw_expected:
        return {"status": "NEEDS_MANUAL", "notes": [f"file is in {kind}/ but defines a {m.group('kw').upper()}"]}
    if m.group("name") != stem:
        return {"status": "NEEDS_MANUAL", "notes": [f"object `{m.group('name')}` doesn't match file name `{stem}`"]}

    head = src[:m.start()]
    header_comments, notes = [], []
    for line in head.splitlines():
        if not LEADING_OK.match(line):
            return {"status": "NEEDS_MANUAL", "notes": [f"unexpected statement before CREATE: {line.strip()[:80]!r}"]}
        if line.strip().startswith("--"):
            header_comments.append(line.rstrip())
        dropped = re.search(r"DROP\s+(?:PROCEDURE|FUNCTION)\s+IF\s+EXISTS\s+`?(\w+)`?", line, re.I)
        if dropped and dropped.group(1) != stem:
            # e.g. API_ChangeUserTheme.sql dropped `ChangeUserTheme`: re-running it failed.
            notes.append(f"old DROP targeted `{dropped.group(1)}`, not `{stem}`; the template drops `{stem}`")

    ends = list(BODY_END.finditer(src, m.end()))
    if not ends:
        return {"status": "NEEDS_MANUAL", "notes": ["no final END ... $$ found"]}
    end = ends[-1]
    tail = src[end.end():]
    if not all(TRAILING_OK.match(l) for l in tail.splitlines()):
        return {"status": "NEEDS_MANUAL", "notes": ["statements after the object's END $$ (more than one object?)"]}

    body = src[m.end():end.start() + 3].rstrip()  # BODY_END starts at the END keyword (any case)
    # Inside the /*!50003 wrapper the body must not contain '*/' anywhere.
    body, converted = _convert_block_comments(body)
    if body is None:
        return {"status": "NEEDS_MANUAL", "notes": [converted]}
    if converted:
        notes.append(f"converted {converted} /* */ comment(s) in the body to -- comments")
    if m.group(2):
        notes.append(f"removed {m.group(2).strip()} (repo convention: no DEFINER)")
    # The body is kept byte-for-byte (no whitespace clean-up), so a conversion's diff shows only
    # the header/footer and any DEFINER / comment change.
    new = "".join(f"{c}\n" for c in header_comments)
    new += (f"/*!50003 DROP {kw_expected} IF EXISTS  `{stem}` */;\n\nDELIMITER $$\n\n"
            f"/*!50003 CREATE {kw_expected} `{stem}`{body} */$$\nDELIMITER ;\n")
    # Whitespace-only differences (trailing spaces, blank lines) don't count: already in template.
    if re.sub(r"\s+", " ", new).strip() == re.sub(r"\s+", " ", src).strip():
        return {"status": "ALREADY_TEMPLATE", "text": src, "notes": []}
    return {"status": "NORMALIZED", "text": new, "notes": notes}


def exists_at(base: str, name: str) -> bool:
    return subprocess.run(["git", "-C", str(DB_REPO), "cat-file", "-e", f"{base}:{name}"],
                          capture_output=True).returncode == 0


def added_text(name: str, base: str, whole: str) -> str:
    """What THIS ticket added to the file: all of it for a new file, else the '+' lines of
    `git diff <base> -- file`. Base = the FB_D commit this ticket started from (state.json
    db.base), so a colleague's earlier lines on the shared DB branch aren't blamed on us."""
    if not exists_at(base, name):
        return whole
    diff = git(DB_REPO, "diff", "-U0", base, "--", name)
    return "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))


def check_file(name: str, db_ticket: str, confirmed: set, base: str, out: dict) -> None:
    path = DB_REPO / name
    m = FOLDER_RE.match(name)
    if not m:
        out["errors"].append(f"{name}: not under Database/<schema>/(SP|Functions|Tables|DDL_DML)/ or not .sql")
        return
    if not path.is_file():
        out["errors"].append(f"{name}: file not found in the DB repo working tree")
        return
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    kind, stem = m.group("kind") or "Script", m.group("stem")
    added = added_text(name, base, text)
    code = strip_comments(added)  # content rules judge only what this ticket added

    # Hygiene (line endings are left to git's autocrlf; working trees on Windows are CRLF)
    if raw.startswith(b"\xef\xbb\xbf"):
        out["warnings"].append(f"{name}: UTF-8 BOM")
    if CONFLICT.search(text):
        out["errors"].append(f"{name}: merge-conflict markers")
    if raw and not raw.endswith(b"\n"):
        out["warnings"].append(f"{name}: no final newline")
    if re.search(r"[ \t]+\r?$", added, re.M):
        out["warnings"].append(f"{name}: trailing whitespace on added lines")

    # Conventions (added lines; the whole file when new)
    if DEFINER.search(code):
        out["errors"].append(f"{name}: DEFINER= clause (repo convention: none)")
    if USE_DB.search(code):
        out["errors"].append(f"{name}: USE <db> statement (repo convention: none)")

    # Structure + naming: every touched SP/function, new or modified, must be exactly in the
    # template (D-52). The normaliser is the single definition of "in the template".
    if kind in ("SP", "Functions"):
        norm = normalize_object(text, kind, stem)
        if norm["status"] == "NORMALIZED":
            out["errors"].append(f"{name}: not in the defined template. Convert it first: "
                                 f"python db_checks.py --normalize {name} --write "
                                 f"({'; '.join(norm['notes']) or 'header/footer only'})")
        elif norm["status"] == "NEEDS_MANUAL":
            out["errors"].append(f"{name}: can't be converted to the template automatically: {'; '.join(norm['notes'])}")
    elif kind == "DDL_DML":
        if not re.fullmatch(rf"{re.escape(db_ticket)}(_\d+)?", stem):
            out["errors"].append(f"{name}: DDL_DML files are named {db_ticket}.sql or {db_ticket}_<n>.sql (D-51)")

    # Destructive statements -> hard stop unless the developer confirmed this file (D-51 q3)
    statements = destructive_statements(code)
    for match in re.finditer(r"\bDROP\s+TABLE\s+IF\s+EXISTS\s+`?(?:tmp|temp)\w*`?", code, re.I):
        out["warnings"].append(f"{name}: {match.group(0)} (scratch table, not treated as destructive)")
    if statements:
        labels = ", ".join(sorted({label for label, _ in statements}))
        token = destructive_token(name, statements)
        if token in confirmed:
            out["warnings"].append(f"{name}: destructive ({labels}), confirmed by developer")
        else:
            out["stops"].append(f"{name}: destructive statement(s): {labels}. Developer confirmation token: {token}")

    # Re-runnability (DDL/DML and table scripts)
    if kind in ("DDL_DML", "Tables", "Script"):
        if CREATE_TABLE_NO_GUARD.search(code):
            out["warnings"].append(f"{name}: CREATE TABLE without IF NOT EXISTS (not re-runnable)")
        for ins in INSERT_NO_GUARD.finditer(code):
            if not re.search(r"\bNOT\s+EXISTS\b|\bIGNORE\b|ON\s+DUPLICATE\s+KEY", ins.group(0), re.I):
                out["warnings"].append(f"{name}: INSERT without NOT EXISTS / IGNORE / ON DUPLICATE KEY guard")
                break
        if ADD_COLUMN.search(code) and not re.search(r"information_schema|IF\s+NOT\s+EXISTS", code, re.I):
            out["warnings"].append(f"{name}: ALTER TABLE ADD COLUMN without an existence check")

    # Dynamic SQL -> the review must check for injection
    if DYNAMIC_SQL.search(code) and re.search(r"CONCAT\s*\(", code, re.I):
        out["warnings"].append(f"{name}: dynamic SQL (PREPARE … FROM with CONCAT): review for injection")


CREATE_ANY = r"CREATE\s+(DEFINER\s*=\s*\S+\s+)?(PROCEDURE|FUNCTION)\s+`?[A-Za-z0-9_]+`?"


def known_db_objects() -> set:
    """Lower-cased SP/function names in the DB repo: file names in SP/Functions folders plus every
    CREATE PROCEDURE/FUNCTION found in any .sql file, on origin/master and in the working tree."""
    names = set()
    listing = git(DB_REPO, "ls-tree", "-r", "--name-only", f"origin/{DB.get('base_branch', 'master')}", "Database")
    listing += git(DB_REPO, "ls-files", "Database") + git(DB_REPO, "ls-files", "--others", "--exclude-standard", "Database")
    for line in listing.splitlines():
        m = FOLDER_RE.match(line.strip())
        if m and m.group("kind") in ("SP", "Functions"):
            names.add(m.group("stem").lower())
    creates = git(DB_REPO, "grep", "-h", "-ioE", CREATE_ANY, f"origin/{DB.get('base_branch', 'master')}", "--", "*.sql")
    creates += git(DB_REPO, "grep", "-h", "-ioE", "--untracked", CREATE_ANY, "--", "*.sql")
    for line in creates.splitlines():
        m = re.search(r"(?:PROCEDURE|FUNCTION)\s+`?([A-Za-z0-9_]+)", line, re.I)
        if m:
            names.add(m.group(1).lower())
    return names


def sps_already_called_on_master() -> set:
    """SP names the WF code on origin/master already calls. A new call to one of these that's
    missing from the DB repo is a known repo gap (~171 legacy SPs exist only in the database,
    2026-09-29), not something this ticket broke."""
    out = git(PROJECT_ROOT, "grep", "-h", "-oE", r'RunSp\w*\(\s*"[A-Za-z0-9_]+"', f"origin/{BASE}", "--", "*.cs")
    return {re.search(r'"(\w+)"', l).group(1).lower() for l in out.splitlines() if '"' in l}


def called_sps_in_wf_diff() -> dict:
    """{sp name: [file:line, ...]} for RunSp*("Name", ...) calls on added lines of the WF diff."""
    base = git(PROJECT_ROOT, "merge-base", f"origin/{BASE}", "HEAD").strip() or f"origin/{BASE}"
    cs_files = [f for f in changed_files() if f.endswith(".cs")]
    calls = {}
    if not cs_files:
        return calls
    diff = git(PROJECT_ROOT, "diff", "-U0", base, "--", *cs_files)
    current, line_no = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("@@"):
            hunk = re.search(r"\+(\d+)", line)
            line_no = int(hunk.group(1)) if hunk else 0
        elif line.startswith("+") and not line.startswith("+++"):
            for m in RUNSP_CALL.finditer(line):
                calls.setdefault(m.group("name"), []).append(f"{current}:{line_no}")
            line_no += 1
    # Untracked new .cs files: the whole file is "added".
    tracked = set(git(PROJECT_ROOT, "ls-files", "--", *cs_files).splitlines())
    for f in cs_files:
        if f not in tracked and (PROJECT_ROOT / f).is_file():
            for i, line in enumerate((PROJECT_ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in RUNSP_CALL.finditer(line):
                    calls.setdefault(m.group("name"), []).append(f"{f}:{i}")
    return calls


def cmd_normalize(files: list, write: bool) -> int:
    """--normalize: show (or with --write, apply) the conversion of each file to the template."""
    results = []
    for name in (f.replace("\\", "/") for f in files):
        m, path = FOLDER_RE.match(name), DB_REPO / name
        if not m or m.group("kind") not in ("SP", "Functions") or not path.is_file():
            results.append({"file": name, "status": "NOT_APPLICABLE"})
            continue
        norm = normalize_object(path.read_text(encoding="utf-8", errors="replace"), m.group("kind"), m.group("stem"))
        row = {"file": name, "status": norm["status"], "notes": norm.get("notes", [])}
        if norm["status"] == "NORMALIZED":
            print(f"--- {name} → template ---")
            for line in norm["text"].splitlines()[:6]:
                print(f"  {line}")
            print("  …")
            for line in norm["text"].splitlines()[-2:]:
                print(f"  {line}")
            for note in norm["notes"]:
                print(f"  note: {note}")
            if write:
                path.write_bytes(norm["text"].encode("utf-8"))  # LF; git's autocrlf decides the checkout form
                row["status"] = "WRITTEN"
        results.append(row)
    blocked = [r for r in results if r["status"] == "NEEDS_MANUAL"]
    status = "NEEDS_MANUAL" if blocked else ("WRITTEN" if write else "PREVIEW")
    print("RESULT: " + json.dumps({"status": status, "files": results}, ensure_ascii=False))
    return 3 if blocked else 0


def detect_db_files(db: dict) -> tuple:
    """(files, problem): the .sql files in the DB repo this ticket changed since db.base (committed or not,
    plus new untracked ones), whether or not they are listed in state.db.files. This is what stops a destructive
    script from escaping the check just by being left out of the list. Not a DB ticket -> ([], None)."""
    if not (db.get("ticket") or db.get("files")):
        return [], None
    base = db.get("base")
    if not base:
        return [], ("db.base is not set, so unlisted DB files can't be detected: set it from `dt db-branch` "
                    "(DB.md: dt state N --set db.base=<head>)")
    if not (DB_REPO / ".git").exists():
        return [], None
    def sql(lines):
        names = (n.strip().replace(chr(92), "/") for n in lines)
        return {n for n in names if n.startswith("Database/") and n.lower().endswith(".sql")}

    # Tracked changes since base (committed or edited) are all this ticket's business. Untracked files are
    # mostly local clutter in the DB repo, so only ones named for this ticket (DDL_DML/{D}.sql, {D}_n.sql) count:
    # a new destructive script is usually exactly that, and it is untracked until step 8 stages it.
    found = sql(git_checked(DB_REPO, "diff", "--name-only", "--diff-filter=ACMR", base).splitlines())
    ticket_stem = re.compile(rf"/DDL_DML/{re.escape(str(db.get('ticket') or ''))}(_\d+)?\.sql$")
    if FLAT:  # flat layout: an untracked script whose name carries the DB ticket's number
        ticket_stem = re.compile(rf"/[^/]*(?<!\d){re.escape(str(db.get('ticket') or ''))}(?!\d)[^/]*\.sql$")
    found |= {n for n in sql(git_checked(DB_REPO, "ls-files", "--others", "--exclude-standard", "Database").splitlines())
              if db.get("ticket") and ticket_stem.search(n)}
    return sorted(found), None


COMMIT_MARK_RE = re.compile(r"re#\d+ - \d+ - ")


def derive_db_files(db: dict) -> dict:
    """The DB files that belong to THIS WF ticket (backlog A-3), instead of a hand-typed list.

    The DB branch FB_{D} is shared by the whole milestone, so ownership comes from the commit message convention
    `re#{D} - {N} - summary` (D = DB ticket, N = WF ticket): the files of the commits since db.base whose message
    starts that way, plus the working tree of a repo that started clean (uncommitted tracked edits, untracked .sql
    under Database/). Commits by other tickets (`re#{D} - {M} - …`) are left out and reported; commits that don't
    follow the convention are reported too, in case they are this ticket's. detect_db_files() stays the wide safety
    scan (every change since db.base is still judged for destructive statements)."""
    ticket, wf, base = db.get("ticket"), db.get("wf_ticket"), db.get("base")
    marker = re.compile(rf"re#{re.escape(str(ticket))} - {re.escape(str(wf))} - ")

    def sql(lines):
        names = (n.strip().replace(chr(92), "/") for n in lines)
        return {n for n in names if n.startswith("Database/") and n.lower().endswith(".sql")}

    mine, others, unconventional = [], [], []
    for line in git_checked(DB_REPO, "log", "--no-merges", "--format=%H%x1f%s", f"{base}..HEAD").splitlines():
        sha, _, subject = line.partition("\x1f")
        if marker.match(subject) or DB.get("branch_per_ticket"):  # own branch per DB ticket: every commit is this ticket's
            mine.append(sha)
        elif COMMIT_MARK_RE.match(subject):
            others.append(f"{sha[:8]} {subject}")
        else:
            unconventional.append(f"{sha[:8]} {subject}")
    files = set()
    for sha in mine:
        files |= sql(git_checked(DB_REPO, "show", "--name-only", "--diff-filter=ACMR", "--format=", sha).splitlines())
    files |= sql(git_checked(DB_REPO, "diff", "--name-only", "--diff-filter=ACMR", "HEAD").splitlines())
    files |= sql(git_checked(DB_REPO, "ls-files", "--others", "--exclude-standard", "Database").splitlines())
    return {"files": sorted(files), "commits": len(mine),
            "other_commits": others[:20], "unconventional_commits": unconventional[:20]}


def git_checked(repo: Path, *args) -> str:
    """Like git() but a failed call raises: an empty answer would read as "no DB changes"."""
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in the DB repo: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def load_ticket_db(ticket: str) -> dict:
    path = PROJECT_ROOT / CONFIG["tickets_dir"] / ticket / "state.json"
    db = (json.loads(path.read_text(encoding="utf-8")).get("db") or {}) if path.is_file() else {}
    return dict(db, wf_ticket=ticket)  # the WF ticket N: derive_db_files() attributes commits by `re#{D} - {N} - `


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticket", help="WF ticket N (reads DEV_TICKETS/N/state.json db.ticket / db.files).")
    parser.add_argument("--db-ticket")
    parser.add_argument("--files", nargs="+", help="DB repo files, relative to the DB repo root.")
    parser.add_argument("--no-call-check", action="store_true", help="Skip the WF-diff SP existence check.")
    parser.add_argument("--base", help="DB repo commit this ticket's changes are measured from "
                                       "(default: state.json db.base, else origin/master).")
    parser.add_argument("--normalize", nargs="+", metavar="DB_FILE",
                        help="Convert SP/function files to the defined template (preview unless --write).")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    args.ticket = ticket_arg(args.ticket)
    args.db_ticket = ticket_arg(args.db_ticket)

    if args.normalize:
        return cmd_normalize(args.normalize, args.write)

    db = load_ticket_db(args.ticket) if args.ticket else {}
    db_ticket = str(args.db_ticket or db.get("ticket") or "")
    files = [f.replace("\\", "/") for f in (args.files or db.get("files") or [])]
    confirmed = set(db.get("destructive_confirmed") or [])
    base = args.base or db.get("base") or f"origin/{DB.get('base_branch', 'master')}"
    out = {"errors": [], "stops": [], "warnings": []}

    if not (DB_REPO / ".git").exists():
        print("RESULT: " + json.dumps({"status": "FAIL", "errors": [f"DB repo not found: {DB_REPO}"]}))
        return 2
    if args.ticket and not args.files:
        detected, problem = detect_db_files(db)
        if problem:
            out["errors"].append(problem)
        unlisted = [f for f in detected if f not in files]
        if unlisted:
            out["warnings"].append(f"not in state.db.files, checked for destructive statements only: "
                                   f"{', '.join(unlisted)} (list the ones that are yours: dt state N --set db.files=...)")
    for name in files:
        check_file(name, db_ticket, confirmed, base, out)
    # Unlisted files may be a colleague's commits on the shared DB branch, so their template/naming problems
    # aren't this ticket's to fix: only a STOP (destructive statement) counts for them, the rest is a warning.
    for name in (unlisted if args.ticket and not args.files else []):
        side = {"errors": [], "stops": [], "warnings": []}
        check_file(name, db_ticket, confirmed, base, side)
        out["stops"] += side["stops"]
        out["warnings"] += [f"(unlisted) {w}" for w in side["errors"] + side["warnings"]]

    missing = {}
    if not args.no_call_check:
        known = known_db_objects()
        legacy = None
        for sp, where in called_sps_in_wf_diff().items():
            if sp.lower() in known:
                continue
            legacy = sps_already_called_on_master() if legacy is None else legacy
            if sp.lower() in legacy:
                out["warnings"].append(f"SP `{sp}` (called at {', '.join(where[:3])}) isn't in the DB repo, but "
                                       f"master already calls it: a known repo gap, not new")
            else:
                missing[sp] = where
                out["errors"].append(f"SP `{sp}` is called in the WF diff ({', '.join(where[:3])}) but exists "
                                     f"neither in the DB repo nor in this ticket's DB files")

    for level in ("stops", "errors", "warnings"):
        for item in out[level]:
            print(f"  {level[:-1].upper():<5} {item}")
    status = "STOP" if out["stops"] else ("FAIL" if out["errors"] else "PASS")
    if args.ticket:  # D-53 quality metrics
        import metrics
        metrics.record(PROJECT_ROOT, CONFIG["tickets_dir"], args.ticket, "db_checks", status=status,
                       errors=len(out["errors"]), stops=len(out["stops"]), warnings=len(out["warnings"]),
                       missing_sps=len(missing))
    print("RESULT: " + json.dumps(dict(status=status, db_ticket=db_ticket, files=files, base=base,
                                       missing_sps=sorted(missing), **out), ensure_ascii=False))
    return {"PASS": 0, "FAIL": 2, "STOP": 3}[status]


if __name__ == "__main__":
    sys.exit(guarded(main))
