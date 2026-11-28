"""Offline tests for jobtracker/candidate_store.py: the local JSON backend
only. No database, no network. Mirrors tests/test_generation_records.py's
style for a Postgres/local-JSON-split store."""
import shutil
import tempfile
import threading
from pathlib import Path

from jobtracker.candidate_store import LocalJSONCandidateProfileStore


def test_get_returns_none_on_empty_database():
    # First-run / fresh deploy: no file, nothing saved yet -- /settings/candidate
    # must render fine against this, not crash on a missing key.
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        assert store.get() is None
        print("PASS test_get_returns_none_on_empty_database")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_candidate_data_round_trips():
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        data = {"personal": {"name": "Ada Lovelace"}, "experience": []}
        store.save_candidate_data(data)

        row = store.get()
        assert row["candidate_data"] == data
        assert row["style_example"] is None
        assert row["updated_at"]
        print("PASS test_save_candidate_data_round_trips")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_style_example_round_trips():
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        store.save_style_example("Dear hiring manager, ...")

        row = store.get()
        assert row["style_example"] == "Dear hiring manager, ..."
        assert row["candidate_data"] is None
        print("PASS test_save_style_example_round_trips")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_saving_one_field_does_not_clobber_the_other():
    # The whole point of two separate save_* methods: uploading a new resume
    # must not wipe out a previously-saved style example, and vice versa.
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        store = LocalJSONCandidateProfileStore(str(tmp / "candidate_profile.json"))
        store.save_candidate_data({"personal": {"name": "Ada Lovelace"}})
        store.save_style_example("Dear hiring manager, ...")

        # re-save the resume -- the letter must survive
        store.save_candidate_data({"personal": {"name": "Ada Lovelace (updated)"}})
        row = store.get()
        assert row["candidate_data"]["personal"]["name"] == "Ada Lovelace (updated)"
        assert row["style_example"] == "Dear hiring manager, ..."

        # re-save the letter -- the resume must survive
        store.save_style_example("Dear team, ...")
        row = store.get()
        assert row["candidate_data"]["personal"]["name"] == "Ada Lovelace (updated)"
        assert row["style_example"] == "Dear team, ..."
        print("PASS test_saving_one_field_does_not_clobber_the_other")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_fresh_store_instance_reads_the_same_file():
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        path = str(tmp / "candidate_profile.json")
        LocalJSONCandidateProfileStore(path).save_candidate_data({"personal": {"name": "Ada"}})

        reloaded = LocalJSONCandidateProfileStore(path)
        assert reloaded.get()["candidate_data"]["personal"]["name"] == "Ada"
        print("PASS test_a_fresh_store_instance_reads_the_same_file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_same_field_saves_do_not_corrupt_the_file():
    # Racing the SAME field against itself can only ever prove "no file
    # corruption" (last-writer-wins on one field is correct by definition,
    # not a sign the lock did anything) -- the race that actually tests the
    # lock's reason for existing is the cross-field one below.
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        path = str(tmp / "candidate_profile.json")
        n_threads = 8

        def _save(i):
            LocalJSONCandidateProfileStore(path).save_style_example(f"letter {i}")

        threads = [threading.Thread(target=_save, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Whichever write landed last, the file must be valid JSON with a
        # real style_example -- not corrupted by an interleaved write.
        row = LocalJSONCandidateProfileStore(path).get()
        assert row["style_example"].startswith("letter ")
        print("PASS test_concurrent_same_field_saves_do_not_corrupt_the_file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_cross_field_saves_do_not_drop_either_field():
    # The actual reason _WRITE_LOCK exists: webapp.py constructs a fresh
    # store instance per request, and the resume upload / cover-letter
    # upload are two SEPARATE save actions that can legitimately race each
    # other (two browser tabs, or one slow LLM-parse request overlapping a
    # quick letter save). Without the lock, a read-modify-write on one field
    # can read a stale snapshot taken before the other field's write landed,
    # and overwrite it back to null -- this races BOTH fields against each
    # other and asserts neither is lost, which a same-field race can't show.
    tmp = Path(tempfile.mkdtemp(prefix="jt_candprofile_"))
    try:
        path = str(tmp / "candidate_profile.json")
        # ONE writer per field, not several: with multiple writers per field,
        # an early writer populates both fields and every later writer reads
        # a snapshot that already has both, so the clobber only shows up in
        # the final state when the LAST writer's read predates the other
        # field's only write -- which needs very few writers. More threads
        # per field makes this look more rigorous while making it strictly
        # blind to the regression it exists to catch (confirmed empirically:
        # 10 per field detects a disabled lock 0/30 runs; 1 per field detects
        # it 30/30 runs).
        n_per_field = 1

        def _save_candidate_data(i):
            LocalJSONCandidateProfileStore(path).save_candidate_data(
                {"personal": {"name": f"candidate {i}"}})

        def _save_style_example(i):
            LocalJSONCandidateProfileStore(path).save_style_example(f"letter {i}")

        threads = (
            [threading.Thread(target=_save_candidate_data, args=(i,)) for i in range(n_per_field)]
            + [threading.Thread(target=_save_style_example, args=(i,)) for i in range(n_per_field)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        row = LocalJSONCandidateProfileStore(path).get()
        assert row["candidate_data"] is not None, "candidate_data was dropped by a racing save"
        assert row["style_example"] is not None, "style_example was dropped by a racing save"
        print("PASS test_concurrent_cross_field_saves_do_not_drop_either_field")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_get_returns_none_on_empty_database()
    test_save_candidate_data_round_trips()
    test_save_style_example_round_trips()
    test_saving_one_field_does_not_clobber_the_other()
    test_a_fresh_store_instance_reads_the_same_file()
    test_concurrent_same_field_saves_do_not_corrupt_the_file()
    test_concurrent_cross_field_saves_do_not_drop_either_field()
    print("\nAll candidate_store tests passed.")
