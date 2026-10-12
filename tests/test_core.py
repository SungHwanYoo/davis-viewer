import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from core import Catalog, EventRenderer, LikeStore, EVENT_DTYPE


def make_session(root: Path, nested: bool = False, *, frame_timestamp_us: int = 1_000_000,
                 exposure_us: int | None = 0, event_records=None) -> Path:
    session = root / "sample_20260926_120000"
    images = session / "nested" if nested else session
    (images / "frames").mkdir(parents=True)
    (images / "events_vis").mkdir()
    (images / "frames" / "frame_000000.png").write_bytes(b"frame")
    (images / "events_vis" / "frame_000000.png").write_bytes(b"vis")
    (session / "meta.json").write_text(json.dumps({"width": 3, "height": 2}), encoding="utf-8")
    with (session / "frames.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "frame_idx", "frame_timestamp_us", "frame_file", "event_vis_file", "exposure_us"
        ])
        writer.writeheader()
        writer.writerow({
            "frame_idx": 0, "frame_timestamp_us": frame_timestamp_us,
            "frame_file": "frames/frame_000000.png",
            "event_vis_file": "events_vis/frame_000000.png",
            "exposure_us": "" if exposure_us is None else exposure_us,
        })
    if event_records is None:
        event_records = [
        (999_000, 0, 0, 1),
        (1_000_100, 1, 0, 1),
        (1_000_200, 1, 0, 0),
        (1_003_000, 2, 1, 1),
        ]
    events = np.array(event_records, dtype=EVENT_DTYPE)
    events.tofile(session / "events.bin")
    return session


class CatalogTests(unittest.TestCase):
    def test_finds_frame_pairs_inside_nested_image_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp), nested=True)
            catalog = Catalog(Path(temp))
            session = catalog.get(session_dir.name)
            self.assertEqual(len(session.rows), 1)
            self.assertEqual(session.image_path(0, "frame").read_bytes(), b"frame")
            self.assertEqual(session.image_path(0, "event").read_bytes(), b"vis")

    def test_lists_real_sessions_with_matching_pairs(self):
        with tempfile.TemporaryDirectory() as temp:
            make_session(Path(temp))
            sessions = Catalog(Path(temp)).list_sessions()
            self.assertEqual([(s["id"], s["frames"]) for s in sessions],
                             [("sample_20260926_120000", 1)])

    def test_excluded_session_is_not_listed_or_accessible(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp))
            catalog = Catalog(Path(temp), excluded_ids={session_dir.name})
            self.assertEqual(catalog.list_sessions(), [])
            with self.assertRaises(KeyError):
                catalog.get(session_dir.name)


class EventRendererTests(unittest.TestCase):
    def test_timestamp_bounds_include_both_ends_without_scanning_full_file(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp))
            renderer = EventRenderer(Catalog(Path(temp)).get(session_dir.name))
            self.assertEqual(renderer.timestamp_bounds(1_000_100, 1_000_200), (1, 3))

    def test_duration_selects_events_and_last_polarity_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp))
            session = Catalog(Path(temp)).get(session_dir.name)
            renderer = EventRenderer(session)
            short = renderer.render_array(0, 0.001)
            self.assertEqual(short[0, 0].tolist(), [255, 255, 255])
            self.assertEqual(short[0, 1].tolist(), [255, 0, 0])
            self.assertEqual(short[1, 2].tolist(), [255, 255, 255])
            long = renderer.render_array(0, 0.01)
            self.assertEqual(long[0, 0].tolist(), [0, 0, 255])
            self.assertEqual(long[1, 2].tolist(), [0, 0, 255])

    def test_30_bins_use_half_open_boundaries_and_fixed_colors(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp), event_records=[
                (899_999, 0, 0, 1),
                (900_000, 0, 0, 1),
                (903_332, 1, 0, 0),
                (903_333, 2, 0, 1),
                (999_999, 1, 1, 0),
                (1_000_000, 0, 1, 0),
            ])
            renderer = EventRenderer(Catalog(Path(temp)).get(session_dir.name))
            batch = renderer.render_30_bins(0)
            self.assertEqual(len(batch.bins), 30)
            self.assertEqual(batch.center_us, 1_000_000)
            self.assertEqual(batch.bins[0].start_offset_us, -100_000)
            self.assertEqual(batch.bins[-1].end_offset_us, 0)
            self.assertEqual([batch.bins[i].event_count for i in (0, 1, 15, 29)], [2, 1, 0, 1])
            self.assertEqual(sum(bin.event_count for bin in batch.bins), 4)
            self.assertEqual((batch.bins[0].on_count, batch.bins[0].off_count), (1, 1))
            first_image = cv2.imdecode(np.frombuffer(batch.bins[0].png, np.uint8), cv2.IMREAD_COLOR)
            empty_image = cv2.imdecode(np.frombuffer(batch.bins[2].png, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(first_image[0, 0].tolist(), [0, 0, 255])
            self.assertEqual(first_image[0, 1].tolist(), [255, 0, 0])
            self.assertTrue(np.all(empty_image == 255))

    def test_30_bins_align_to_exposure_midpoint_and_old_session_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            recent = make_session(Path(temp) / "recent", exposure_us=40_000, event_records=[
                (919_999, 0, 0, 1), (920_000, 1, 0, 1),
                (1_019_999, 2, 0, 0), (1_020_000, 0, 1, 1),
            ])
            batch = EventRenderer(Catalog(recent.parent).get(recent.name)).render_30_bins(0)
            self.assertEqual((batch.center_us, batch.center_source), (1_020_000, "exposure_midpoint"))
            self.assertEqual((batch.bins[0].event_count, batch.bins[29].event_count), (1, 1))
            old = make_session(Path(temp) / "old", exposure_us=None, event_records=[
                (900_000, 0, 0, 1), (1_000_000, 1, 0, 0),
            ])
            old_batch = EventRenderer(Catalog(old.parent).get(old.name)).render_30_bins(0)
            self.assertEqual((old_batch.center_us, old_batch.center_source),
                             (1_000_000, "frame_timestamp_fallback"))
            self.assertEqual(sum(bin.event_count for bin in old_batch.bins), 1)

    def test_30_bins_keep_empty_edges_near_recording_start_and_end(self):
        with tempfile.TemporaryDirectory() as temp:
            start = make_session(Path(temp) / "start", frame_timestamp_us=50_000, event_records=[
                (0, 0, 0, 1), (25_000, 1, 0, 0), (50_000, 2, 0, 1),
            ])
            start_batch = EventRenderer(Catalog(start.parent).get(start.name)).render_30_bins(0)
            self.assertTrue(all(bin.event_count == 0 for bin in start_batch.bins[:15]))
            self.assertEqual(sum(bin.event_count for bin in start_batch.bins), 2)
            end = make_session(Path(temp) / "end", frame_timestamp_us=1_050_000, event_records=[
                (950_000, 0, 0, 1), (999_999, 1, 0, 0),
            ])
            end_batch = EventRenderer(Catalog(end.parent).get(end.name)).render_30_bins(0)
            self.assertTrue(all(bin.event_count == 0 for bin in end_batch.bins[15:]))
            self.assertEqual(sum(bin.event_count for bin in end_batch.bins), 2)

    def test_30_bins_reuse_cached_result_without_second_raw_window_read(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = make_session(Path(temp))
            renderer = EventRenderer(Catalog(Path(temp)).get(session_dir.name))
            with patch.object(renderer, "_read_interval", wraps=renderer._read_interval) as read:
                first = renderer.render_30_bins(0)
                second = renderer.render_30_bins(0)
            self.assertIs(first, second)
            self.assertEqual(read.call_count, 1)


class LikeStoreTests(unittest.TestCase):
    def test_one_favorite_per_frame_and_any_second_click_cancels_it(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LikeStore(Path(temp) / "likes.sqlite3")
            self.assertEqual(store.toggle("sample", 3, "visitor-a"), (True, 1))
            self.assertEqual(store.toggle("sample", 3, "visitor-b"), (False, 0))
            self.assertEqual(store.counts("sample", "visitor-a"), {})
            self.assertEqual(store.toggle("sample", 3, "visitor-b"), (True, 1))
            self.assertEqual(store.counts("sample", "visitor-a"), {3: (1, False)})

    def test_legacy_duplicate_votes_become_one_favorite_with_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "likes.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE likes (
                    session_id TEXT NOT NULL, frame_idx INTEGER NOT NULL,
                    voter_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, frame_idx, voter_id))""")
                db.executemany("INSERT INTO likes VALUES (?, ?, ?)", [
                    ("sample", 3, "visitor-a"), ("sample", 3, "visitor-b"),
                    ("sample", 4, "visitor-c"),
                ])
            store = LikeStore(path)
            self.assertEqual(store.counts("sample", "visitor-b"),
                             {3: (1, True), 4: (1, False)})
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM likes WHERE frame_idx=3").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM likes_dedup_archive").fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO likes VALUES ('sample', 4, 'visitor-d')")
            self.assertEqual(store.toggle("sample", 3, "visitor-a"), (False, 0))

    def test_first_login_claims_existing_vote_without_changing_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "likes.sqlite3"
            store = LikeStore(path)
            store.toggle("sample", 3, "visitor-a")
            store.create_account("tester_a", "1234")
            self.assertIsNone(store.login("tester_a", "wrong", "visitor-a"))
            token, account = store.login("tester_a", "1234", "visitor-a")
            self.assertEqual(account["username"], "tester_a")
            self.assertEqual(store.counts("sample", account["voter_id"]), {3: (1, True)})
            self.assertEqual(store.likers("sample", 3),
                             {"count": 1, "users": ["tester_a"], "anonymous_count": 0})
            self.assertEqual(store.toggle("sample", 3, account["voter_id"]), (False, 0))
            self.assertEqual(store.toggle("sample", 3, account["voter_id"]), (True, 1))
            self.assertEqual(store.account_for_token(token)["username"], "tester_a")
            store.logout(token)
            self.assertIsNone(store.account_for_token(token))
            self.assertEqual(LikeStore(path).likers("sample", 3)["users"], ["tester_a"])

if __name__ == "__main__":
    unittest.main()
