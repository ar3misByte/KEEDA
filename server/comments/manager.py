"""Object-attached comments and their discussion threads.

A comment always knows what it refers to - a schematic symbol, a PCB
footprint, a net, a sheet - so this is review, not chat. Threads are one level
deep: a comment on an object, and replies to it. That covers every review
conversation this needs and keeps the UI readable.

Comments live in KiCad Live's own database, never inside the KiCad files, so
commenting can never modify or corrupt a design.
"""
from __future__ import annotations

import logging

from server.activity.event_manager import EventManager
from server.database.db import Database

log = logging.getLogger("kicadlive.comments")

VALID_DOMAINS = ("schematic", "pcb")
VALID_STATUSES = ("open", "resolved", "reopened")
MAX_TEXT = 4000


class CommentError(ValueError):
    pass


class CommentManager:
    def __init__(self, database: Database, events: EventManager):
        self.db = database
        self.events = events

    # ------------------------------------------------------------- create

    def create(self, project_id: str, domain: str, object_id: str, text: str,
               author_id: str, author_name: str, object_type: str | None = None,
               object_ref: str | None = None, sheet: str | None = None,
               parent_id: str | None = None) -> dict:
        if domain not in VALID_DOMAINS:
            raise CommentError(f"domain must be one of {VALID_DOMAINS}")
        cleaned = (text or "").strip()
        if not cleaned:
            raise CommentError("comment text cannot be empty")
        if len(cleaned) > MAX_TEXT:
            raise CommentError(f"comment text must be under {MAX_TEXT} characters")
        if not object_id:
            raise CommentError("a comment must reference an object")

        if parent_id:
            parent = self.db.comment(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise CommentError("parent comment not found")
            if parent.get("parent_id"):
                # Keep threads one level deep: a reply to a reply still belongs
                # to the original thread.
                parent_id = parent["parent_id"]
                parent = self.db.comment(parent_id)
            # A reply inherits its thread's target, so a thread can never span
            # two different objects.
            domain = parent["domain"]
            object_id = parent["object_id"]
            object_type = parent["object_type"]
            object_ref = parent["object_ref"]
            sheet = parent["sheet"]

        comment = self.db.add_comment(
            project_id=project_id, domain=domain, object_id=object_id, text=cleaned,
            author_id=author_id, author_name=author_name, object_type=object_type,
            object_ref=object_ref, sheet=sheet, parent_id=parent_id)

        label = object_ref or object_id[:8]
        self.events.record(
            project_id=project_id, domain="collab",
            action="comment_replied" if parent_id else "comment_created",
            description=(f"replied to the comment on {label}" if parent_id
                         else f"commented on {label}: {_preview(cleaned)}"),
            user_id=author_id, username=author_name,
            object_type=object_type, object_id=object_id, object_ref=object_ref)
        return comment

    # ------------------------------------------------------------ resolve

    def set_status(self, project_id: str, comment_id: str, status: str,
                   user_id: str, username: str) -> dict:
        if status not in VALID_STATUSES:
            raise CommentError(f"status must be one of {VALID_STATUSES}")
        comment = self.db.comment(comment_id)
        if comment is None or comment["project_id"] != project_id:
            raise CommentError("comment not found")
        if comment.get("parent_id"):
            raise CommentError("replies cannot be resolved; resolve the thread instead")

        updated = self.db.set_comment_status(comment_id, status, user_id)
        label = comment.get("object_ref") or comment["object_id"][:8]
        self.events.record(
            project_id=project_id, domain="collab",
            action="comment_resolved" if status == "resolved" else "comment_reopened",
            description=(f"resolved the comment on {label}" if status == "resolved"
                         else f"reopened the comment on {label}"),
            user_id=user_id, username=username,
            object_type=comment.get("object_type"), object_id=comment["object_id"],
            object_ref=comment.get("object_ref"))
        return updated

    # ------------------------------------------------------------- listing

    def threads(self, project_id: str, status: str = "all", domain: str | None = None,
                object_id: str | None = None, author_id: str | None = None,
                viewer_id: str | None = None) -> list[dict]:
        """Comment threads, newest activity first.

        `status` is one of all/open/resolved. `author_id` filters to threads
        started by that user; `viewer_id` additionally marks threads the viewer
        participates in, which is what the "Mine" filter uses.
        """
        everything = self.db.comments(project_id, domain=domain)
        roots = [c for c in everything if not c.get("parent_id")]
        replies_by_parent: dict[str, list[dict]] = {}
        for comment in everything:
            parent = comment.get("parent_id")
            if parent:
                replies_by_parent.setdefault(parent, []).append(comment)

        threads = []
        for root in roots:
            if object_id and root["object_id"] != object_id:
                continue
            if status == "open" and root["status"] not in ("open", "reopened"):
                continue
            if status == "resolved" and root["status"] != "resolved":
                continue
            if author_id and root["author_id"] != author_id:
                continue

            replies = sorted(replies_by_parent.get(root["comment_id"], []),
                             key=lambda c: c["created_at"])
            participants = {root["author_id"]} | {r["author_id"] for r in replies}
            thread = dict(root)
            thread["replies"] = replies
            thread["reply_count"] = len(replies)
            thread["last_activity"] = replies[-1]["created_at"] if replies else root["created_at"]
            thread["participants"] = sorted(participants)
            thread["is_mine"] = bool(viewer_id and viewer_id in participants)
            threads.append(thread)

        threads.sort(key=lambda t: t["last_activity"], reverse=True)
        return threads

    def for_object(self, project_id: str, domain: str, object_id: str) -> list[dict]:
        return self.threads(project_id, status="all", domain=domain, object_id=object_id)

    def counts(self, project_id: str) -> dict:
        threads = self.threads(project_id)
        open_threads = [t for t in threads if t["status"] in ("open", "reopened")]
        return {
            "total": len(threads),
            "open": len(open_threads),
            "resolved": len(threads) - len(open_threads),
        }

    def annotated_objects(self, project_id: str) -> dict[str, dict]:
        """`{object_id: {open, total}}`, so views can badge commented objects."""
        summary: dict[str, dict] = {}
        for thread in self.threads(project_id):
            entry = summary.setdefault(thread["object_id"],
                                       {"open": 0, "total": 0,
                                        "reference": thread.get("object_ref"),
                                        "domain": thread["domain"]})
            entry["total"] += 1
            if thread["status"] in ("open", "reopened"):
                entry["open"] += 1
        return summary


def _preview(text: str, limit: int = 60) -> str:
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= limit else single_line[: limit - 1] + "…"
