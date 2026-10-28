"""Create a Zotero parent item from an orphan vault PDF, and link the file.

The forward verb beyond ``zotlink`` (which moves or removes a pointer to an
item that already exists). Everything here mints something in Zotero, so the
whole subsystem is built around one question: *can this run twice?*

The answer is the attempt store (:mod:`zotcreate.attempts`), reserved before
the first POST. Creation is TWO unkeyed POSTs — a parent, then a linked-file
attachment under its returned key — so there are two lost-response windows,
not one, and the store records each stage separately.
"""
