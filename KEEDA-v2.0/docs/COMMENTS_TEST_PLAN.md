# Comments — Test Plan

Status recorded: **2026-09-18**. Nothing is marked PASS that was not observed.

```powershell
python -m pytest tests\unit\test_activity_comments.py -q
python -m pytest tests\integration\test_collab_integration.py -q -k comment
```

---

## C1 — A comment is attached to a real object

**SETUP** Server running, two clients connected.
**ACTION** Aditya comments on schematic symbol R1.
**EXPECTED** The comment records domain, object id, object reference and author.
**ACTUAL** Stored with `domain: schematic`, `object_ref: R1`, author `Aditya`,
status `open`.
**PASS** — comments are review, not chat: every one knows what it refers to.

## C2 — Other clients are notified live

**ACTION** Aditya comments; Rahul is watching.
**EXPECTED** Rahul receives `comment_created` without polling.
**ACTUAL** Received over the existing WebSocket, with updated open count 1.
**PASS**

## C3 — Empty text is refused

**ACTION** Post a comment of `"   "`.
**EXPECTED** Rejected; the connection survives.
**ACTUAL** `error / invalid_message`; a following heartbeat was still answered.
**PASS**

## C4 — An invalid domain is refused

**ACTION** Comment with `domain: "gerber"`.
**ACTUAL** `CommentError`.
**PASS**

## C5 — A comment must name an object

**ACTION** Comment with an empty `object_id`.
**ACTUAL** `CommentError`.
**PASS** — this is what stops comments degenerating into generic chat.

## C6 — Replies thread correctly

**ACTION** Aditya comments on R1; Rahul replies.
**EXPECTED** One thread with one reply, in order.
**ACTUAL** `reply_count: 1`, reply authored by Rahul.
**PASS**

## C7 — Threads stay one level deep

**ACTION** Reply to a reply.
**EXPECTED** It joins the original thread rather than nesting further.
**ACTUAL** `parent_id` rewritten to the root; `reply_count: 2`.
**PASS** — keeps a review conversation readable.

## C8 — A reply cannot retarget a thread

**ACTION** Reply to a PCB thread while claiming `domain: schematic` and a
different object.
**EXPECTED** The reply inherits the thread's target.
**ACTUAL** Reply stored with the root's `object_id` and `domain`.
**PASS** — a thread can never span two objects.

## C9 — Resolve and reopen

**ACTION** Resolve the thread, then reopen it.
**EXPECTED** Status transitions; open count follows.
**ACTUAL** `resolved` → open count 0; `reopened` → open count 1. Both broadcast
as `comment_updated`.
**PASS**

## C10 — Replies cannot be resolved individually

**ACTION** Resolve a reply.
**ACTUAL** `CommentError` ("resolve the thread instead").
**PASS**

## C11 — Filters

**ACTION** Filter All / Open / Resolved / by domain.
**ACTUAL** One resolved and one open thread filtered correctly; the domain
filter returned only the schematic thread.
**PASS**

## C12 — "Mine" includes participation, not just authorship

**ACTION** Rahul replies to Aditya's thread; view as Rahul.
**EXPECTED** The thread counts as Rahul's.
**ACTUAL** `is_mine: true` for the replier; `false` for an unrelated viewer.
**PASS**

## C13 — Commenting emits activity

**ACTUAL** Timeline entry `commented on R1: check this`, action
`comment_created`.
**PASS**

## C14 — Comments persist across a server restart

**ACTION** Post a comment, disconnect, connect a fresh client.
**EXPECTED** The new client receives existing threads on connect.
**ACTUAL** The R1 thread was delivered in the `comments` message after `hello`.
**PASS** — comments live in SQLite, never in the KiCad files.

## C15 — Objects carry comment badges

**ACTION** Two comments on one footprint; request annotated objects.
**ACTUAL** `{open: 2}` for that object, used to badge the Schematic/PCB pages.
**PASS**

## C16 — Comments never touch the design files

**ACTION** Comment on schematic and PCB objects; inspect the project files.
**EXPECTED** No modification.
**ACTUAL** Unchanged. Comments are stored only in `data/kicadlive.db`.
**PASS** — commenting can never corrupt a design.

---

## Results

| ID | Test | Result |
|---|---|---|
| C1 | Object-attached comment | PASS |
| C2 | Live notification | PASS |
| C3–C5 | Validation (empty, domain, object) | PASS |
| C6–C8 | Threading and target inheritance | PASS |
| C9–C10 | Resolution rules | PASS |
| C11–C12 | Filters including "Mine" | PASS |
| C13 | Activity integration | PASS |
| C14 | Persistence | PASS |
| C15 | Badge counts | PASS |
| C16 | Design files untouched | PASS |

## Not run

* Five separate computers — see `docs/FIVE_COMPUTER_TEST.md`.
* @mentions and per-user notification routing. Not implemented: the dashboard
  shows every comment event to everyone in the project.
