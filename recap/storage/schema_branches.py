"""Git-like branching for the Recap schema registry.

Adapted from GitLake (arXiv:2607.08319), a "Git-for-data" design that lifts
snapshots into lakehouse-wide commits, branches, and merges so agents can
work on isolated branches while humans review and publish changes.

This module keeps GitLake's core branch/commit/merge governance and
substitutes its auxiliary machinery with target-native equivalents (Mode 2,
adapted port):

* GitLake's Iceberg snapshot internals are replaced by Recap's existing
  versioned-JSON artifact model -- every published ``{name}/{version}.json``
  in :class:`RegistryStorage` is already a commit-like immutable snapshot, so
  the I/O contract (write/publish a versioned artifact) lines up directly.
* GitLake's agent/pipeline orchestration and its preliminary Alloy
  correctness model are intentionally out of scope: this is the governance
  layer over Recap's registry, not a re-implementation of the lakehouse.

Branch state is kept in a sibling ``.branches`` namespace on the same fsspec
filesystem as the registry so branches stay invisible to published listings
until an explicit merge publishes a branch's tip as a new version.
"""

import json
from pathlib import Path
from urllib.parse import quote_plus, unquote_plus

from recap.storage.merge_conflicts import MergeConflict, detect_merge_conflict
from recap.storage.registry import RegistryStorage
from recap.types import RecapType, from_dict, to_dict

# Hidden namespace (kept out of RegistryStorage.ls() by the dot prefix) where
# branch metadata and per-branch commit snapshots live.
_BRANCH_NAMESPACE = ".branches"


class SchemaBranches:
    """Branch/commit/merge governance over a :class:`RegistryStorage`.

    A *branch* is an isolated line of edits off a published base version of a
    schema. Edits are appended as immutable *commits* (snapshots). A *merge*
    publishes the branch's tip back to the main registry as a single new
    version, so a branch's changes become visible atomically or not at all.
    """

    def __init__(self, storage: RegistryStorage):
        self.storage = storage
        self.fs = storage.fs
        self.branch_root = f"{storage.root_path}/{_BRANCH_NAMESPACE}"

    def _branch_dir(self, name: str, branch_name: str) -> str:
        return f"{self.branch_root}/{quote_plus(name)}/{quote_plus(branch_name)}"

    def _meta_path(self, name: str, branch_name: str) -> str:
        return f"{self._branch_dir(name, branch_name)}/meta.json"

    def _commits_dir(self, name: str, branch_name: str) -> str:
        return f"{self._branch_dir(name, branch_name)}/commits"

    def _read_meta(self, name: str, branch_name: str) -> dict:
        try:
            with self.fs.open(self._meta_path(name, branch_name)) as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Branch {branch_name!r} does not exist for {name!r}"
            )

    def _commit_indexes(self, name: str, branch_name: str) -> list[int]:
        try:
            return sorted(
                int(Path(path).stem)
                for path in self.fs.ls(self._commits_dir(name, branch_name))
            )
        except FileNotFoundError:
            return []

    def branch(
        self,
        name: str,
        branch_name: str,
        base_version: int | None = None,
    ) -> int:
        """Create a branch off ``base_version`` (default: latest published).

        Returns the base version the branch was created from. Raises
        ``ValueError`` if there is no published version to branch from and
        ``FileExistsError`` if the branch already exists.
        """
        if base_version is None:
            base_version = self.storage.latest(name)
        if base_version is None:
            raise ValueError(
                f"Cannot branch {name!r}: no published version to branch from"
            )

        meta_path = self._meta_path(name, branch_name)
        if self.fs.exists(meta_path):
            raise FileExistsError(f"Branch {branch_name!r} already exists for {name!r}")

        self.fs.mkdirs(self._commits_dir(name, branch_name), exist_ok=True)
        with self.fs.open(meta_path, "w") as f:
            json.dump({"base_version": base_version, "merged": False}, f)
        return base_version

    def commit(self, name: str, branch_name: str, type_: RecapType) -> int:
        """Append an immutable snapshot to a branch and return its index."""
        meta = self._read_meta(name, branch_name)
        if meta["merged"]:
            raise ValueError(
                f"Branch {branch_name!r} is already merged; "
                "create a new branch to publish further changes"
            )
        indexes = self._commit_indexes(name, branch_name)
        index = (indexes[-1] + 1) if indexes else 1
        with self.fs.open(
            f"{self._commits_dir(name, branch_name)}/{index}.json", "w"
        ) as f:
            json.dump(to_dict(type_), f)
        return index

    def tip(self, name: str, branch_name: str) -> tuple[RecapType, int]:
        """Return ``(snapshot, commit_index)`` at the head of the branch.

        With no commits the tip is the published base snapshot (index 0).
        """
        indexes = self._commit_indexes(name, branch_name)
        if indexes:
            with self.fs.open(
                f"{self._commits_dir(name, branch_name)}/{indexes[-1]}.json"
            ) as f:
                return from_dict(json.load(f)), indexes[-1]
        base_version = self._read_meta(name, branch_name)["base_version"]
        result = self.storage.get(name, base_version)
        if result is None:
            raise FileNotFoundError(
                f"Base version {base_version} for {name!r} is missing"
            )
        return result[0], 0

    def log(self, name: str, branch_name: str) -> dict:
        """Return ``{base_version, merged, commits}`` where ``commits`` is an
        ordered list of ``(index, snapshot)`` tuples."""
        meta = self._read_meta(name, branch_name)
        commits: list[tuple[int, RecapType]] = []
        for index in self._commit_indexes(name, branch_name):
            with self.fs.open(
                f"{self._commits_dir(name, branch_name)}/{index}.json"
            ) as f:
                commits.append((index, from_dict(json.load(f))))
        return {
            "base_version": meta["base_version"],
            "merged": meta["merged"],
            "commits": commits,
        }

    def branches(self, name: str) -> list[str]:
        """List branch names for a schema."""
        try:
            return sorted(
                unquote_plus(Path(path).name)
                for path in self.fs.ls(f"{self.branch_root}/{quote_plus(name)}")
            )
        except FileNotFoundError:
            return []

    def merge(self, name: str, branch_name: str) -> int:
        """Publish the branch tip to the main registry as a new version.

        The publish is a single ``put`` so the branch's changes become visible
        atomically. Requires at least one commit (an empty branch has nothing
        to publish) and a branch that has not already been merged.

        If the main registry advanced past the branch's base version while the
        branch was open, the tip is reconciled against those concurrent changes
        with a three-way merge (base version vs. current latest vs. branch tip).
        Overlapping, incompatible edits raise :class:`MergeConflict` instead of
        silently overwriting the concurrent work.
        """
        meta = self._read_meta(name, branch_name)
        if meta["merged"]:
            raise ValueError(f"Branch {branch_name!r} is already merged")
        if not self._commit_indexes(name, branch_name):
            raise ValueError(f"Branch {branch_name!r} has no commits; nothing to merge")
        type_, _ = self.tip(name, branch_name)

        base_version = meta["base_version"]
        current = self.storage.get(name)
        if current is not None and current[1] != base_version:
            base_result = self.storage.get(name, base_version)
            base_type = base_result[0] if base_result else None
            if conflicts := detect_merge_conflict(base_type, current[0], type_):
                raise MergeConflict(conflicts)

        new_version = self.storage.put(name, type_)
        meta["merged"] = True
        with self.fs.open(self._meta_path(name, branch_name), "w") as f:
            json.dump(meta, f)
        return new_version
