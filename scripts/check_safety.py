"""Refuses a release if the add-on could change anyone's collection.

People rely on their Anki collections, and every release installs itself on
everyone's computer, so this runs before every release (release.sh) and fails
if the add-on code could add, change or delete cards, notes, decks or review
history. The rules:

1. The collection is only reached through stats.ReadOnly, which allows single
   SELECT queries and nothing else.
2. mw.col is only checked for None or handed to functions in stats.py.
3. Inside stats.py, `col` is only used for col.sched.day_cutoff and to build
   ReadOnly.
4. No Anki method that edits a collection is called anywhere, no write SQL
   appears in any string, and Anki's operation and collection modules are
   never imported.
5. Only updater.py installs anything, and only this add-on.

Usage: python3 scripts/check_safety.py [addon folder]   (exit code 1 on any problem)
"""

import ast
import pathlib
import re
import sys

ADDON = pathlib.Path(__file__).resolve().parent.parent / "addon"

# Anki methods that edit a collection (current and legacy names).
MUTATING = {
    "execute", "executemany", "executescript", "commit", "rollback",
    "add_note", "add_notes", "addNote", "remove_notes", "remNotes", "remove_cards",
    "remCards", "remove_cards_and_orphaned_notes", "remove_deck", "remove_decks",
    "rem", "update_card", "update_cards", "update_note", "update_notes",
    "update_deck", "update_deck_config", "set_deck", "setDeck", "set_due_date",
    "reschedule_cards_as_new", "forget_cards", "forgetCards", "suspend_cards",
    "unsuspend_cards", "bury_cards", "unbury_cards", "add_tags", "remove_tags",
    "bulk_add", "bulk_remove", "find_and_replace", "findReplace", "set_config",
    "set_config_bool", "remove_config", "save", "flush", "autosave", "close",
    "close_for_full_sync", "reopen", "mod_schema", "modSchema", "set_schema_modified",
    "undo", "redo", "merge_undo_entries", "add_custom_undo_entry", "import_anki_package",
    "import_csv", "export_anki_package", "fix_integrity", "optimize", "full_upload",
    "full_download", "sync_collection", "after_note_updates", "set_user_flag_for_cards",
    "empty_filtered_deck", "rebuild_filtered_deck", "add_normal_deck_with_name",
    "id_for_name", "rename", "reposition_new_cards", "sort_cards", "randomize_cards",
}
WRITE_SQL = re.compile(
    r"\b(insert\s+(or\s+\w+\s+)?into|update\s+\w+\s+set|delete\s+from|drop\s+(table|index)"
    r"|alter\s+table|replace\s+into|create\s+(table|index|trigger|view)|attach\s+database"
    r"|vacuum|pragma\s+\w+\s*=)",
    re.IGNORECASE,
)
FORBIDDEN_IMPORTS = ("anki", "aqt.operations", "sqlite3")


def check_file(path: pathlib.Path) -> list:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    problems = []

    def where(node):
        return f"{path.name}:{node.lineno}"

    def inside(node, cls_name, fn_name):
        n = node
        fn = cls = None
        while n in parents:
            n = parents[n]
            if isinstance(n, ast.FunctionDef) and fn is None:
                fn = n.name
            if isinstance(n, ast.ClassDef):
                cls = n.name
                break
        return cls == cls_name and fn == fn_name

    def in_class(node, cls_name):
        n = node
        while n in parents:
            n = parents[n]
            if isinstance(n, ast.ClassDef):
                return n.name == cls_name
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    problems.append(f"{where(node)} imports {name}")

        if isinstance(node, ast.Attribute):
            if node.attr in MUTATING:
                problems.append(f"{where(node)} calls .{node.attr}, which can edit a collection")
            if node.attr == "db" and not (path.name == "stats.py" and inside(node, "ReadOnly", "__init__")):
                problems.append(f"{where(node)} reaches .db outside stats.ReadOnly")
            if node.attr == "install" and path.name != "updater.py":
                problems.append(f"{where(node)} installs something outside updater.py")

            # mw.col: only `mw.col is None` checks or passing it to stats.*
            if node.attr == "col" and isinstance(node.value, ast.Name) and node.value.id == "mw":
                parent = parents.get(node)
                ok = isinstance(parent, ast.Compare) or (
                    isinstance(parent, ast.Call)
                    and node in parent.args
                    and isinstance(parent.func, ast.Attribute)
                    and isinstance(parent.func.value, ast.Name)
                    and parent.func.value.id == "stats"
                )
                if not ok:
                    problems.append(f"{where(node)} uses mw.col other than a None check or stats.*(mw.col)")

            # col.<x> in stats.py: only col.sched.day_cutoff and col.db inside ReadOnly
            if isinstance(node.value, ast.Name) and node.value.id == "col":
                if path.name != "stats.py":
                    problems.append(f"{where(node)} uses col.{node.attr} outside stats.py")
                elif node.attr == "sched":
                    parent = parents.get(node)
                    if not (isinstance(parent, ast.Attribute) and parent.attr == "day_cutoff"):
                        problems.append(f"{where(node)} uses col.sched for something other than day_cutoff")
                elif node.attr != "db":
                    problems.append(f"{where(node)} uses col.{node.attr}")

        # Passing col anywhere in stats.py other than ReadOnly(col) / anki_today(col).
        if path.name == "stats.py" and isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == "col":
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    if name not in ("ReadOnly", "anki_today"):
                        problems.append(f"{where(node)} passes col to {name}()")

        if isinstance(node, ast.Constant) and isinstance(node.value, str) and WRITE_SQL.search(node.value):
            # The gate's own list of refused keywords is the one allowed mention.
            if not (path.name == "stats.py" and in_class(node, "ReadOnly")):
                problems.append(f"{where(node)} contains write SQL: {node.value.strip()[:60]!r}")

    return problems


def main() -> int:
    folder = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ADDON
    files = sorted(folder.glob("*.py"))
    problems = [p for f in files for p in check_file(f)]
    if problems:
        print("Safety check FAILED. The add-on could change someone's collection:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"Safety check passed: {len(files)} files only read from collections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
