from recap.storage.merge_conflicts import MergeConflict, detect_merge_conflict
from recap.types import IntType, StringType, StructType


def test_no_common_base_and_heads_agree_is_clean():
    ours = StructType(fields=[IntType(bits=32, name="id")])
    theirs = StructType(fields=[IntType(bits=32, name="id")])

    assert detect_merge_conflict(None, ours, theirs) == []


def test_no_common_base_and_heads_disagree_conflicts():
    ours = StructType(fields=[IntType(bits=32, name="id")])
    theirs = StructType(fields=[StringType(bytes_=8, name="id")])

    conflicts = detect_merge_conflict(None, ours, theirs)

    assert conflicts == ["snapshot: no common base to merge"]


def test_only_one_side_diverged_is_a_fast_forward():
    base = StructType(fields=[IntType(bits=32, name="id")])
    ours = StructType(fields=[IntType(bits=32, name="id")])  # unchanged
    theirs = StructType(fields=[IntType(bits=64, name="id")])  # branch widened it

    assert detect_merge_conflict(base, ours, theirs) == []


def test_both_sides_converged_to_same_result_is_clean():
    base = StructType(fields=[IntType(bits=32, name="id")])
    ours = StructType(fields=[IntType(bits=64, name="id")])
    theirs = StructType(fields=[IntType(bits=64, name="id")])

    assert detect_merge_conflict(base, ours, theirs) == []


def test_overlapping_field_change_is_reported_by_name():
    base = StructType(fields=[IntType(bits=32, name="id")])
    ours = StructType(fields=[StringType(bytes_=8, name="id")])
    theirs = StructType(fields=[IntType(bits=64, name="id")])

    conflicts = detect_merge_conflict(base, ours, theirs)

    assert conflicts == ["field 'id': changed on both base and branch"]


def test_non_overlapping_field_changes_merge_cleanly():
    base = StructType(fields=[IntType(bits=32, name="id"), StringType(name="note")])
    # ours widens only "note", theirs widens only "id" -> no field overlaps.
    ours = StructType(
        fields=[IntType(bits=32, name="id"), StringType(bytes_=16, name="note")]
    )
    theirs = StructType(fields=[IntType(bits=64, name="id"), StringType(name="note")])

    assert detect_merge_conflict(base, ours, theirs) == []


def test_unnamed_fields_fall_back_to_positional_keys():
    base = StructType(fields=[IntType(bits=32)])
    ours = StructType(fields=[StringType(bytes_=8)])
    theirs = StructType(fields=[IntType(bits=64)])

    conflicts = detect_merge_conflict(base, ours, theirs)

    assert conflicts == ["field '#0': changed on both base and branch"]


def test_non_struct_snapshots_fall_back_to_whole_comparison():
    base = IntType(bits=32)
    ours = IntType(bits=64)
    theirs = StringType(bytes_=8)

    conflicts = detect_merge_conflict(base, ours, theirs)

    assert conflicts == ["snapshot: base version advanced with overlapping edits"]


def test_merge_conflict_exposes_descriptions_and_message():
    error = MergeConflict(["field 'id': changed on both base and branch"])

    assert error.conflicts == ["field 'id': changed on both base and branch"]
    assert "Merge conflict" in str(error)
    assert "field 'id'" in str(error)
