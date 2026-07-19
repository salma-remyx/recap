import pytest

from recap.storage.merge_conflicts import MergeConflict
from recap.storage.registry import RegistryStorage
from recap.storage.schema_branches import SchemaBranches
from recap.types import IntType, StringType, StructType


@pytest.fixture
def storage(tmp_path):
    return RegistryStorage(f"file://{tmp_path}")


def test_branch_defaults_to_latest_published_version(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))  # v1

    base = SchemaBranches(storage).branch(name, "feature")

    assert base == 1


def test_branch_requires_a_published_version(storage):
    with pytest.raises(ValueError):
        SchemaBranches(storage).branch("missing", "feature")


def test_branch_name_collision_is_rejected(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")

    with pytest.raises(FileExistsError):
        branches.branch(name, "feature")


def test_commits_are_isolated_from_published_versions(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))  # v1
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")
    evolved = StructType(fields=[IntType(bits=48), StringType(bytes_=8)])

    index = branches.commit(name, "feature", evolved)

    assert index == 1
    # The committed edit is NOT visible on the main registry yet.
    assert storage.versions(name) == [1]
    # The branch plumbing is hidden from the published schema listing.
    assert storage.ls() == [name]


def test_log_records_base_and_commits(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))  # v1
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")
    evolved = StructType(fields=[IntType(bits=48), StringType(bytes_=8)])

    branches.commit(name, "feature", evolved)

    log = branches.log(name, "feature")
    assert log["base_version"] == 1
    assert log["merged"] is False
    assert [index for index, _ in log["commits"]] == [1]
    assert log["commits"][0][1] == evolved


def test_merge_publishes_tip_as_new_version_and_marks_merged(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))  # v1
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")
    evolved = StructType(fields=[IntType(bits=48), StringType(bytes_=8)])
    branches.commit(name, "feature", evolved)

    new_version = branches.merge(name, "feature")

    assert new_version == 2
    assert storage.versions(name) == [1, 2]
    merged_type, merged_version = storage.get(name)
    assert merged_version == 2
    assert merged_type == evolved
    assert branches.log(name, "feature")["merged"] is True


def test_cannot_commit_or_merge_after_merge(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")
    branches.commit(name, "feature", StructType(fields=[IntType(bits=64)]))
    branches.merge(name, "feature")

    with pytest.raises(ValueError):
        branches.commit(name, "feature", StructType(fields=[IntType(bits=8)]))
    with pytest.raises(ValueError):
        branches.merge(name, "feature")


def test_merge_requires_at_least_one_commit(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32)]))
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")

    with pytest.raises(ValueError):
        branches.merge(name, "feature")


def test_merge_rejects_overlapping_concurrent_change(storage):
    name = "orders"
    # v1 is the common ancestor with a single named field.
    storage.put(name, StructType(fields=[IntType(bits=32, name="id")]))
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")  # forks off v1
    # The branch widens "id".
    branches.commit(name, "feature", StructType(fields=[IntType(bits=64, name="id")]))
    # Meanwhile another writer publishes an incompatible edit to "id" as v2.
    storage.put(name, StructType(fields=[StringType(bytes_=8, name="id")]))

    with pytest.raises(MergeConflict):
        branches.merge(name, "feature")
    # The concurrent v2 is left intact -- the merge did not clobber it.
    assert storage.versions(name) == [1, 2]


def test_merge_allows_non_overlapping_concurrent_change(storage):
    name = "orders"
    storage.put(name, StructType(fields=[IntType(bits=32, name="id")]))
    branches = SchemaBranches(storage)
    branches.branch(name, "feature")  # forks off v1
    # The branch touches only "id".
    branches.commit(name, "feature", StructType(fields=[IntType(bits=64, name="id")]))
    # A concurrent writer adds a different field, leaving "id" untouched.
    storage.put(
        name,
        StructType(fields=[IntType(bits=32, name="id"), StringType(name="note")]),
    )

    new_version = branches.merge(name, "feature")

    assert new_version == 3
    assert storage.versions(name) == [1, 2, 3]
