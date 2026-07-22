"""Three-way merge conflict detection for schema branches.

Adapted from GitLake (arXiv:2607.08319), a "Git-for-data" design whose central
correctness guarantee is that a branch merge publishes atomically *and* refuses
to silently clobber changes that landed on the base since the branch forked. In
GitLake this is expressed as a merge over overlapping snapshot deltas; a merge
that touches state another writer already changed must halt with a conflict
rather than overwrite it.

Recap's registry keeps each ``{name}/{version}.json`` as an immutable snapshot,
so the branch layer forks off a *base version* and merges by publishing a new
version. Without a check, a merge overwrites whatever became the latest version
in the meantime -- the classic lost-update. This module supplies the missing
piece: a Git-style three-way comparison between the common ancestor (the base
version the branch forked from), *ours* (the current published latest), and
*theirs* (the branch tip). A conflict is reported only when both sides diverged
from the ancestor in an overlapping, incompatible way; a stale base that does
not actually overlap the branch's edits still merges cleanly.

The comparison is field-aware for :class:`StructType` (the common registry
shape): it reports the specific field names that both sides changed
incompatibly. For any other shape it falls back to a whole-snapshot comparison.
"""

from recap.types import RecapType, StructType


class MergeConflict(Exception):
    """Raised when a three-way merge detects overlapping concurrent edits.

    ``conflicts`` holds one human-readable description per overlapping change
    so callers (e.g. the registry API) can surface exactly what collided.
    """

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__("Merge conflict: " + "; ".join(conflicts))


def _field_key(field: RecapType, position: int) -> str:
    """Identify a struct field by its name, falling back to its position.

    Named fields are matched by name across the three snapshots so a reorder
    isn't mistaken for a change; unnamed fields fall back to positional keys.
    """
    name = field.extra_attrs.get("name")
    return name if name is not None else f"#{position}"


def _field_map(type_: StructType) -> dict[str, RecapType]:
    return {_field_key(field, i): field for i, field in enumerate(type_.fields)}


def detect_merge_conflict(
    base: RecapType | None,
    ours: RecapType,
    theirs: RecapType,
) -> list[str]:
    """Return the overlapping conflicts between ``ours`` and ``theirs``.

    ``base`` is the common ancestor (the branch's base version), ``ours`` is the
    current published latest, and ``theirs`` is the branch tip. An empty list
    means the merge is safe to publish.

    The merge is clean whenever only one side diverged from the ancestor
    (fast-forward), when neither did, or when both converged to the same result.
    A conflict is reported only where both sides changed the same thing to
    different values.
    """
    if base is None:
        # No common ancestor to diff against; if the two heads disagree we
        # cannot safely reconcile them.
        return [] if ours == theirs else ["snapshot: no common base to merge"]

    # Only one side (or neither) moved off the ancestor -> nothing overlaps.
    if ours == base or theirs == base or ours == theirs:
        return []

    if (
        isinstance(base, StructType)
        and isinstance(ours, StructType)
        and isinstance(theirs, StructType)
    ):
        return _struct_conflicts(base, ours, theirs)

    # Both sides diverged and we can't reason field-by-field: whole snapshot.
    return ["snapshot: base version advanced with overlapping edits"]


def _struct_conflicts(
    base: StructType,
    ours: StructType,
    theirs: StructType,
) -> list[str]:
    base_map = _field_map(base)
    ours_map = _field_map(ours)
    theirs_map = _field_map(theirs)

    conflicts: list[str] = []
    for key in sorted(set(ours_map) | set(theirs_map) | set(base_map)):
        b = base_map.get(key)
        o = ours_map.get(key)
        t = theirs_map.get(key)
        ours_changed = o != b
        theirs_changed = t != b
        if ours_changed and theirs_changed and o != t:
            conflicts.append(f"field {key!r}: changed on both base and branch")
    return conflicts
