# Obsidian — what the agent can reach, and why the rest is closed

**Status:** decisions, taken during the pre-Phase-4 hardening pass.
**Scope:** the capability surface JARVIS's model can invoke against a connected
vault. Not the API surface, which is wider by design.

---

## 0. The distinction this document rests on

There are two different questions, and the audit conflated them:

1. **Can JARVIS do this at all?** — a question about the provider and the API.
2. **Can the *model* decide to do this?** — a question about the tool registry.

A capability can be fully implemented, permission-checked, audited and exposed
over HTTP, and still deliberately have no tool. The user pressing a button in
the Obsidian panel has decided to delete a note. A model that inferred deletion
from a sentence has not; it has produced a plausible next action. The gap
between those two is the entire safety argument, so the answer to "why isn't
this a tool?" is usually not "we forgot".

The audit found six capabilities the agent cannot reach. Each is classified
below as **intentional**, **accidental**, or **undocumented** — the last
meaning the decision was right but had never been written down, which is how it
came to look like an oversight.

---

## 1. The surface, decided

| Capability | Provider | API route | Agent tool | Classification |
|---|---|---|---|---|
| search | ✅ | ✅ | `search_obsidian` | exposed |
| read | ✅ | ✅ | `read_obsidian_note` | exposed |
| list notes / folders | ✅ | ✅ | `list_obsidian_notes` | exposed |
| links + backlinks | ✅ | ✅ | `obsidian_note_links` | **accidental — now exposed** |
| create | ✅ | ✅ | `create_obsidian_note` | exposed, always confirmed |
| update | ✅ | ✅ | `update_obsidian_note` | exposed, always confirmed |
| status | ✅ | ✅ | `obsidian_status` | exposed |
| metadata | ✅ | via read | ❌ | intentional — redundant |
| delete | ✅ | ✅ | ❌ | intentional — irreversible |
| move / rename | ✅ | ❌ | ❌ | intentional for the agent; API gap noted |
| sync (plan / pull) | ✅ | ✅ | ❌ | intentional — deferred, staleness exposed instead |
| conflict list | ✅ | ✅ | ❌ | undocumented — deferred with sync |
| conflict resolve | ✅ | ✅ | ❌ | intentional — the decision is the user's |
| connect / disconnect | ✅ | ✅ | ❌ | intentional — configuration, not work |

`tests/test_obsidian_agent.py` pins this table: one test asserts the exact set
of Obsidian tool names, another asserts that no tool named
`delete_obsidian_note`, `move_obsidian_note`, `sync_obsidian`,
`resolve_obsidian_conflict` or `disconnect_obsidian` exists, and a third
asserts no Obsidian tool declares a capability above `WRITE`. Adding one of
these later fails those tests first, which forces whoever adds it back to this
document.

---

## 2. The reasoning, per capability

### 2.1 Links and backlinks — **accidental omission, now closed**

This was the one real miss. Outgoing links are in the note's own text, so
reading it is sufficient. **Incoming** links are a property of every *other*
note; `VaultTransport.backlinks` computes them by scanning the vault, and no
combination of `search_obsidian` and `read_obsidian_note` reconstructs them
reliably. The agent was therefore unable to answer "what else did I write about
this?" — a question the vault's structure exists to answer.

It is a pure read: it opens files, writes nothing, and goes through the same
`ObsidianService.guard("read", …)` as every other read, so taint, the operator
switches, the permission engine and the `OBSIDIAN_ACTION` audit all apply
unchanged. Exposed as `obsidian_note_links`.

### 2.2 Metadata — **intentional, redundant**

`ObsidianProvider.metadata()` returns path, title, folder, frontmatter, tags,
aliases, links, size, mtime and content hash. `read_obsidian_note` already
returns the **raw file including its frontmatter block** as the tool's content,
plus path, title, tags and content hash as structured data. A separate tool
would add a second way to get information the model already has, at the cost of
one more thing for it to choose between. Not exposed.

### 2.3 Delete — **intentional, and this is the important one**

Deleting a note removes a file the user wrote. `VaultTransport.delete` unlinks
it; there is no trash, no undo token, and no version history unless the user
happens to keep the vault in git. It is the single most irreversible thing this
subsystem can do.

The permission model would handle it correctly — `DELETE` is its own capability,
the irreversibility floor forces `ALLOW` down to `ASK`, and
`ObsidianService.guard` checks the separate `allow_deletes` operator switch
before the engine is even consulted. That is precisely the argument *for*
leaving it closed rather than against it: a tool would be safe only because
three independent mechanisms all held, and the user's confirmation would be the
last of them. Deletion should require a human to go and do it, not a human to
fail to stop it.

Available at `DELETE /api/obsidian/notes/{path}`, where the caller is a person
who navigated to it. Not a tool.

### 2.4 Move / rename — **intentional for the agent**

Moving a note breaks every wikilink that resolved to its old path. Obsidian
itself rewrites those links on rename; JARVIS does not — `transport.move`
relocates the file and nothing else. An agent-initiated move would therefore
silently damage the link graph, which is a slow, hard-to-notice loss rather
than an obvious one. Not exposed.

Worth recording separately: `move` has **no API route either**, so it is
currently unreachable outside Python. That is an API gap rather than a safety
one, it is not a Phase 4 blocker, and it is listed in the hardening report's
remaining-gaps section rather than fixed here.

### 2.5 Sync — **intentional, deferred; staleness exposed instead**

Sync is a one-way pull: it reads the vault and writes JARVIS's own documents and
chunks. It touches nothing in the vault, so it is not dangerous in the way
delete is. Two reasons it is still not a tool:

- **It is unbounded work.** `plan()` walks up to 5,000 notes and `pull()`
  ingests and embeds every changed one. That belongs to an explicit user action
  with visible progress, not to the middle of a conversational turn where its
  only visible effect is a long pause.
- **A sync triggered as a side effect of a question is a background writer the
  user did not ask for.** The audit's finding 9 asked whether the absence of
  automatic sync was intentional. It is: there is no watcher, no timer, and no
  sync on read. `test_nothing_syncs_as_a_side_effect_of_reading` proves it by
  running a read, a search and a list and asserting `last_synced_at` is still
  null afterwards.

The real cost of that choice is that JARVIS's *index* can be stale while the
vault is current — a search can miss a note written five minutes ago, and
"I found nothing" is indistinguishable from "you have no such note". So the
staleness is now surfaced rather than left implicit: `obsidian_status` reports
`last_synced_at`, the number of indexed documents, and a sentence the model can
repeat — either "the search index has never been synced from this vault" or
"the search index was last synced about N day(s) ago … JARVIS does not sync by
itself." Reads, lists and backlinks go straight to the filesystem and are always
current; only search is index-backed, and the status tool says which is which.

Revisit if and when sync is incremental enough to be sub-second on a typical
vault, at which point a `sync_obsidian` tool becomes a reasonable read-only
addition.

### 2.6 Conflicts — **listing deferred, resolution closed**

The conflict model is three hashes: the base JARVIS ingested, the vault's
current content, and JARVIS's pending local version. A conflict means both
sides changed since the base.

`ObsidianSync.resolve` is not exposed, and its own docstring says why —
*"Never chooses on their behalf."* `KEEP_JARVIS` overwrites the user's note with
JARVIS's version wholesale; `MERGE` rewrites the note to hold both under
headings. Both destroy or restructure something the user wrote, and which side
is right is exactly the judgement a model cannot make: it has no access to what
the user meant by their edit. Closed.

Listing conflicts is harmless and is available at
`GET /api/obsidian/conflicts`. It is not a tool only because conflicts arise
from sync, and sync is deferred — a conflict-listing tool for a subsystem the
agent cannot invoke would be a control with nothing behind it. It moves when
sync moves.

### 2.7 Connect / disconnect — **intentional**

Choosing which folder on disk JARVIS may read is a configuration decision that
defines the whole subsystem's blast radius. A tool that could repoint the vault
would let a model widen its own access, which is the inversion the permission
model exists to prevent. Panel only.

---

## 3. Attachments and binary files

A vault holds more than Markdown: images, PDFs, audio, anything dragged into
it. JARVIS handles only `.md` and `.markdown` (`NOTE_SUFFIXES`), and:

- **Listing and sync skip them.** `iter_notes` filters by suffix, so
  attachments never appear in `list_obsidian_notes`, never reach search, and
  are never ingested. This is intentional — extracting text from a PDF or
  running OCR on a screenshot is a content-ingestion feature, not part of a
  vault connector.
- **Writes to them are refused.** `_writable_path` rejects any non-Markdown
  suffix: *"JARVIS only writes Markdown notes (.md) into a vault."*
- **Reads are now refused too.** This was a real defect the audit's finding 7
  pointed at. `VaultTransport.read` decoded whatever bytes it found with
  `errors="replace"`, so reading `Attachments/diagram.png` returned a page of
  U+FFFD replacement characters *presented as the note's content*. A model
  shown that has no way to distinguish it from a note the user wrote badly.
  `read` now raises a `VaultError` naming the file as an attachment.
  `test_an_attachment_is_refused_not_decoded` pins it.

Attachment *ingestion* remains unimplemented and out of scope. What changed is
only that its absence is now stated rather than silently producing garbage.

---

## 4. `/api/obsidian/audit` is not user-scoped, and cannot be

The audit's finding 6 asked whether the missing `user_id` filter on
`GET /api/obsidian/audit` was intentional. The answer is structural rather than
a matter of intent: **`activity_logs` has no `user_id` column.** There is
nothing to filter on. JARVIS is single-user by construction —
`JarvisCore.ensure_default_user` resolves the subject as the first row of
`users` and its docstring says so explicitly — and the activity log describes
one person's system.

Every other reader of that table behaves the same way, including
`ActivityService.recent`, which backs the live activity feed. Adding a filter to
one route while the schema has no such column would be theatre.

`test_the_activity_log_has_no_user_column` asserts the absence. If a second
subject is ever introduced, that column has to arrive with it, the test fails,
and this route is one of the places that must be revisited before it starts
leaking. Recorded as a prerequisite of multi-user support, not as a defect to
fix now.
