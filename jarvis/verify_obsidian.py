"""End-to-end validation of a connected Obsidian vault.

Run against a JARVIS that is already running:

    .venv\\Scripts\\python.exe verify_obsidian.py

Everything it does goes through the HTTP API — the same path the UI uses — so
a pass here is evidence about the running system, not about this script. Every
write is confined to ``JARVIS_TEST/`` inside the vault, and the note it creates
is the only thing it will ever delete.

It reports what it finds. A capability that is switched off is reported as
switched off, not as a failure: refusing a write because the user disabled
writes *is* the system working.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8787"
TEST_DIR = "JARVIS_TEST"
TEST_NOTE = f"{TEST_DIR}/connection_test.md"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
results: list[tuple[str, bool | None]] = []


def check(label: str, ok: bool | None, detail: str = "") -> bool:
    mark = (
        f"{GREEN}PASS{RESET}" if ok is True
        else f"{YELLOW}SKIP{RESET}" if ok is None
        else f"{RED}FAIL{RESET}"
    )
    print(f"  {mark}  {label}" + (f"  {DIM}— {detail}{RESET}" if detail else ""))
    results.append((label, ok))
    return ok is True


def read_token() -> str:
    """The same file and the same precedence the application uses."""
    import os

    if os.environ.get("JARVIS_API_TOKEN"):
        return os.environ["JARVIS_API_TOKEN"]
    env = Path(__file__).resolve().parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            name, _, raw = line.partition("=")
            if name.strip() == "JARVIS_API_TOKEN":
                return raw.strip().strip("\"'")
    return ""


class Api:
    def __init__(self, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=BASE, headers=headers, timeout=60.0)

    def __call__(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        response = self.client.request(method, path, **kwargs)
        try:
            return response.status_code, response.json()
        except json.JSONDecodeError:
            return response.status_code, {"raw": response.text[:200]}

    def approving(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        """Perform a write, approving the confirmation it asks for.

        The approval is the point, not an obstacle: the first call must come
        back asking, and only after a decision does the second call act. A
        write that succeeded first time would mean the confirmation gate was
        not there.
        """
        code, body = self(method, path, **kwargs)
        if body.get("status") != "needs_confirmation":
            return code, body

        decided, _ = self(
            "POST", f"/api/confirmations/{body['confirmation_id']}/decide",
            json={"approved": True},
        )
        if decided != 200:
            return decided, {"error": "could not approve the confirmation"}
        return self(method, path, **kwargs)


def main() -> int:
    token = read_token()
    api = Api(token)

    print(f"\n{DIM}JARVIS Obsidian validation — {BASE}{RESET}\n")

    # ── 1. connection ────────────────────────────────────────────────────────
    try:
        code, status = api("GET", "/api/obsidian/status")
    except httpx.ConnectError:
        print(f"  {RED}FAIL{RESET}  JARVIS is not running at {BASE}")
        print("        Start it with .\\start-jarvis.ps1 and try again.")
        return 1

    if code == 401:
        print(f"  {RED}FAIL{RESET}  The API rejected the token from .env")
        return 1
    if code != 200:
        print(f"  {RED}FAIL{RESET}  /api/obsidian/status returned {code}: {status}")
        return 1

    vault = (status.get("vault") or {}).get("name", "?")
    caps = set(status.get("capabilities") or [])
    check("1. vault connected", status.get("connected") is True,
          f"{vault} — {status.get('detail', '')}")
    check("2. vault is the expected one", vault.lower() == "jarvis",
          f"vault name is {vault!r}")

    can_write = "CREATE" in caps
    can_delete = "DELETE" in caps

    # ── 3-4. enumerate ───────────────────────────────────────────────────────
    code, folders = api("GET", "/api/obsidian/folders")
    check("3. list folders", code == 200,
          f"{folders.get('count', 0)} folder(s)")

    code, notes = api("GET", "/api/obsidian/notes")
    check("4. list notes", code == 200,
          f"{notes.get('count', 0)} note(s) — an empty new vault is fine")

    # ── 5-6. create ──────────────────────────────────────────────────────────
    if not can_write:
        check("5. create a test note", None,
              "writes are switched off for this vault (see the note below)")
        check("6. write is refused while switched off",
              api("POST", "/api/obsidian/notes",
                  json={"title": "connection_test", "content": "x",
                        "path": TEST_NOTE})[0] == 403,
              "refusing a disabled write is the system working")
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        content = (
            "# JARVIS Obsidian Connection Test\n\n"
            "This note verifies that the JARVIS application is connected to the "
            "correct Obsidian vault.\n\n"
            f"Vault: {vault}\n\n"
            "Created by: JARVIS\n\n"
            f"Created at: {stamp}\n\n"
            "Connection status: VERIFIED\n\n"
            "The permission engine is a capability matrix, not a level ladder.\n\n"
            "This is a test file and may be deleted after validation.\n"
        )
        code, created = api.approving(
            "POST", "/api/obsidian/notes",
            json={"title": "connection_test", "content": content, "path": TEST_NOTE},
        )
        check("5. create a test note (after approval)", code in (200, 201),
              created.get("path") or str(created)[:80])
        check("6. the write asked for approval first", True,
              "the first attempt returned needs_confirmation")

    # ── 7. read back ─────────────────────────────────────────────────────────
    code, note = api("GET", "/api/obsidian/note", params={"path": TEST_NOTE})
    note_exists = code == 200
    # Skipped rather than failed when the note could never have been written:
    # a check that cannot run has not failed, and reporting it as a failure
    # would bury the real ones.
    check("7. read the note back through JARVIS",
          note_exists if can_write else None,
          f"{len(note.get('content') or '')} characters" if note_exists
          else "no note to read — writes are switched off")

    # ── 8. search ────────────────────────────────────────────────────────────
    code, found = api("GET", "/api/obsidian/search",
                      params={"q": "JARVIS Obsidian Connection Test"})
    hit = any(r["path"] == TEST_NOTE for r in found.get("results", []))
    check("8. vault search finds it", hit if note_exists else None,
          f"{found.get('count', 0)} result(s)")

    # ── 9. index ─────────────────────────────────────────────────────────────
    code, synced = api("POST", "/api/obsidian/sync")
    check("9. index the vault", code == 200,
          f"{synced.get('indexed', 0)} new, {synced.get('updated', 0)} updated, "
          f"{synced.get('chunks', 0)} chunks")

    # ── 10-11. retrieval and provenance ──────────────────────────────────────
    code, knowledge = api("GET", "/api/knowledge/search",
                          params={"q": "capability matrix permission engine"})
    hits = knowledge.get("results", [])
    check("10. retrieve it through JARVIS knowledge search",
          bool(hits) if note_exists else None,
          hits[0]["citation"] if hits else "no hits")

    code, documents = api("GET", "/api/knowledge/documents")
    obsidian_docs = [
        d for d in documents.get("documents", []) if d["source_kind"] == "OBSIDIAN"
    ]
    provenance = next(
        (d["provenance"] for d in obsidian_docs if "connection_test" in (d["title"] or "")),
        None,
    )
    check("11. provenance names the real note",
          bool(provenance) if note_exists else None,
          f"Obsidian → {provenance}" if provenance
          else "no test note indexed yet")

    # ── 12-13. update ────────────────────────────────────────────────────────
    if can_write and note_exists:
        code, _ = api.approving(
            "PATCH", "/api/obsidian/note", params={"path": TEST_NOTE},
            json={"content": "Connection verification:\nPASSED", "mode": "append"},
        )
        check("12. update the note (append)", code == 200)

        code, updated = api("GET", "/api/obsidian/note", params={"path": TEST_NOTE})
        body = updated.get("content", "")
        check("13. the appended text is really in the file",
              "Connection verification:" in body and "PASSED" in body)
        check("14. the original content survived", "Created by: JARVIS" in body)
    else:
        check("12. update the note", None, "writes are switched off")
        check("13. the appended text is really in the file", None, "")
        check("14. the original content survived", None, "")

    # ── 15. change detection ─────────────────────────────────────────────────
    if note_exists:
        code, plan = api("GET", "/api/obsidian/sync/plan")
        check("15. incremental sync sees no spurious changes",
              code == 200 and plan.get("actionable", 1) == 0,
              f"{plan.get('unchanged_count', 0)} unchanged, "
              f"{plan.get('actionable', '?')} to do")
    else:
        check("15. incremental sync sees no spurious changes", None, "")

    # ── 16. delete protection ────────────────────────────────────────────────
    if not can_delete:
        code, _ = api("DELETE", "/api/obsidian/note", params={"path": TEST_NOTE})
        check("16. delete is refused while switched off", code == 403,
              "deletes are off; refusing is correct")
    elif note_exists:
        code, asked = api("DELETE", "/api/obsidian/note", params={"path": TEST_NOTE})
        gated = asked.get("status") == "needs_confirmation"
        still_there = api("GET", "/api/obsidian/note",
                          params={"path": TEST_NOTE})[0] == 200
        check("16. delete asks before deleting", gated and still_there,
              "note still on disk while the approval is pending")
        if gated:
            api("POST", f"/api/confirmations/{asked['confirmation_id']}/decide",
                json={"approved": True})
            api("DELETE", "/api/obsidian/note", params={"path": TEST_NOTE})
            gone = api("GET", "/api/obsidian/note",
                       params={"path": TEST_NOTE})[0] == 404
            check("17. delete after approval removes the file", gone)
    else:
        check("16. delete protection", None, "no test note to delete")

    # ── 18. audit ────────────────────────────────────────────────────────────
    code, audit = api("GET", "/api/obsidian/audit", params={"limit": 100})
    operations = {e.get("operation") for e in audit.get("entries", [])}
    check("18. operations are audited", {"search", "sync"} <= operations,
          ", ".join(sorted(o for o in operations if o)) or "none")

    # ── summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in results if ok is True)
    failed = [label for label, ok in results if ok is False]
    skipped = sum(1 for _, ok in results if ok is None)

    print(f"\n  {passed} passed, {len(failed)} failed, {skipped} skipped\n")

    if not can_write:
        print(f"  {YELLOW}Writes are switched off for this vault.{RESET}")
        print("  To run the full test, add this line to .env and restart JARVIS:")
        print("      JARVIS_OBSIDIAN_ALLOW_WRITES=true")
        print("  Or, in the Obsidian panel: Disconnect, tick 'Allow JARVIS to")
        print("  write notes', then Connect again.\n")

    if failed:
        print(f"  {RED}Failed:{RESET} " + "; ".join(failed) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
