"""Read-only DAVIS recording catalog, event rendering, and local likes."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
import unicodedata
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


EVENT_DTYPE = np.dtype([
    ("timestamp", "<i8"),
    ("x", "<u2"),
    ("y", "<u2"),
    ("polarity", "u1"),
])


@dataclass(frozen=True)
class FrameRow:
    index: int
    timestamp_us: int
    exposure_us: int | None
    frame_path: Path
    event_path: Path
    event_count: int

    def public(self) -> dict:
        return {
            "index": self.index,
            "timestamp_us": self.timestamp_us,
            "event_count": self.event_count,
        }


@dataclass(frozen=True)
class EventBin:
    index: int
    start_offset_us: int
    end_offset_us: int
    event_count: int
    on_count: int
    off_count: int
    png: bytes

    def public(self) -> dict:
        return {
            "index": self.index,
            "start_offset_us": self.start_offset_us,
            "end_offset_us": self.end_offset_us,
            "event_count": self.event_count,
            "on_count": self.on_count,
            "off_count": self.off_count,
        }


@dataclass(frozen=True)
class BinnedFrame:
    frame_index: int
    center_us: int
    center_source: str
    bins: tuple[EventBin, ...]

    def public(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "center_us": self.center_us,
            "center_source": self.center_source,
            "window_us": 100_000,
            "brightness": "fixed",
            "bins": [event_bin.public() for event_bin in self.bins],
        }


@dataclass(frozen=True)
class Session:
    id: str
    directory: Path
    width: int
    height: int
    rows: tuple[FrameRow, ...]

    @property
    def bin_path(self) -> Path:
        return self.directory / "events.bin"

    def row(self, index: int) -> FrameRow:
        if index < 0 or index >= len(self.rows) or self.rows[index].index != index:
            raise KeyError(index)
        return self.rows[index]

    def image_path(self, index: int, kind: str) -> Path:
        row = self.row(index)
        if kind == "frame":
            return row.frame_path
        if kind == "event":
            return row.event_path
        raise KeyError(kind)

    def public(self) -> dict:
        timestamps = [row.timestamp_us for row in self.rows]
        return {
            "id": self.id,
            "frames": len(self.rows),
            "duration_s": round((timestamps[-1] - timestamps[0]) / 1_000_000, 1) if timestamps else 0,
            "width": self.width,
            "height": self.height,
        }


class Catalog:
    def __init__(self, root: Path, excluded_ids=()):
        self.root = Path(root).expanduser().resolve()
        self.excluded_ids = frozenset(excluded_ids)
        self._sessions: dict[str, Session] = {}
        if not self.root.is_dir():
            raise FileNotFoundError(f"Recording directory does not exist: {self.root}")
        for directory in sorted(self.root.iterdir()):
            if directory.name not in self.excluded_ids and directory.is_dir() and all((directory / name).is_file() for name in
                                          ("frames.csv", "meta.json", "events.bin")):
                session = self._load(directory)
                if session.rows:
                    self._sessions[session.id] = session

    def _resolve_image(self, directory: Path, relative: str, expected: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 2 or path.parts[0] != expected:
            raise ValueError(f"Unsafe image path in {directory / 'frames.csv'}: {relative}")
        for base in (directory, *(p for p in directory.iterdir() if p.is_dir())):
            candidate = base / path
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"Missing image in {directory}: {relative}")

    def _load(self, directory: Path) -> Session:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        rows = []
        with (directory / "frames.csv").open(newline="", encoding="utf-8-sig") as file:
            for record in csv.DictReader(file):
                index = int(record["frame_idx"])
                if index != len(rows):
                    raise ValueError(f"Nonsequential frame index in {directory}: {index}")
                rows.append(FrameRow(
                    index=index,
                    timestamp_us=int(record["frame_timestamp_us"]),
                    exposure_us=(int(record["exposure_us"])
                                 if record.get("exposure_us") not in (None, "") else None),
                    frame_path=self._resolve_image(directory, record["frame_file"], "frames"),
                    event_path=self._resolve_image(directory, record["event_vis_file"], "events_vis"),
                    event_count=int(record.get("event_count") or 0),
                ))
        return Session(directory.name, directory, int(meta["width"]), int(meta["height"]), tuple(rows))

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def list_sessions(self) -> list[dict]:
        return [session.public() for session in self._sessions.values()]


class EventRenderer:
    def __init__(self, session: Session):
        self.session = session
        size = session.bin_path.stat().st_size
        if size % EVENT_DTYPE.itemsize:
            raise ValueError(f"Truncated event file: {session.bin_path}")
        self.events = (np.memmap(session.bin_path, dtype=EVENT_DTYPE, mode="r")
                       if size else np.empty(0, dtype=EVENT_DTYPE))

    def timestamp_bounds(self, start_us: int, end_us: int) -> tuple[int, int]:
        # bisect reads O(log n) elements. np.searchsorted copies this strided
        # memmap field and scans multi-gigabyte event files before searching.
        timestamps = self.events["timestamp"]
        return bisect_left(timestamps, start_us), bisect_right(timestamps, end_us)

    def _read_interval(self, start_us: int, end_us: int) -> np.ndarray:
        """Read one half-open raw event window into memory exactly once."""
        timestamps = self.events["timestamp"]
        first = bisect_left(timestamps, start_us)
        last = bisect_left(timestamps, end_us)
        return np.array(self.events[first:last], copy=True)

    def _encode_bin(self, events: np.ndarray) -> bytes:
        width, height = self.session.width, self.session.height
        canvas = np.full((height * width, 3), 255, dtype=np.uint8)
        if len(events):
            xs, ys = events["x"], events["y"]
            valid = (xs < width) & (ys < height)
            pixels = (ys[valid].astype(np.int32) * width + xs[valid]).astype(np.int32)
            polarity = events["polarity"][valid]
            if len(pixels):
                unique_pixels, positions_from_end = np.unique(pixels[::-1], return_index=True)
                latest_polarity = polarity[::-1][positions_from_end]
                # White background, full-intensity red ON and blue OFF for
                # every bin. There is no per-bin contrast normalization.
                canvas[unique_pixels] = np.where(
                    latest_polarity[:, None] == 1,
                    np.array([0, 0, 255], dtype=np.uint8),
                    np.array([255, 0, 0], dtype=np.uint8),
                )
        ok, encoded = cv2.imencode(".png", canvas.reshape((height, width, 3)),
                                   [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            raise RuntimeError("Could not encode event bin")
        return encoded.tobytes()

    @lru_cache(maxsize=12)
    def render_30_bins(self, frame_index: int) -> BinnedFrame:
        row = self.session.row(frame_index)
        center_source = "exposure_midpoint" if row.exposure_us is not None else "frame_timestamp_fallback"
        center = row.timestamp_us + (row.exposure_us or 0) // 2
        start = center - 100_000
        end = center
        events = self._read_interval(start, end)
        boundaries = np.array([start + index * 100_000 // 30 for index in range(31)], dtype=np.int64)
        timestamps = np.ascontiguousarray(events["timestamp"])
        positions = np.searchsorted(timestamps, boundaries, side="left")
        bins = []
        for index in range(30):
            chunk = events[int(positions[index]):int(positions[index + 1])]
            polarity = chunk["polarity"]
            bins.append(EventBin(
                index=index,
                start_offset_us=int(boundaries[index] - center),
                end_offset_us=int(boundaries[index + 1] - center),
                event_count=len(chunk),
                on_count=int(np.count_nonzero(polarity == 1)),
                off_count=int(np.count_nonzero(polarity == 0)),
                png=self._encode_bin(chunk),
            ))
        return BinnedFrame(frame_index, center, center_source, tuple(bins))

    def render_array(self, frame_index: int, duration_s: float) -> np.ndarray:
        if not math.isfinite(duration_s) or not 0.001 <= duration_s <= 3.0:
            raise ValueError("Event window must be between 0.001 and 3 seconds")
        row = self.session.row(frame_index)
        center = row.timestamp_us + (row.exposure_us or 0) // 2
        half = round(duration_s * 500_000)
        first, last = self.timestamp_bounds(center - half, center + half)
        width, height = self.session.width, self.session.height
        canvas = np.full((height * width, 3), 255, dtype=np.uint8)
        for start in range(first, last, 250_000):
            chunk = self.events[start:min(start + 250_000, last)]
            xs, ys, polarity = chunk["x"], chunk["y"], chunk["polarity"]
            valid = (xs < width) & (ys < height)
            pixels = (ys[valid].astype(np.int32) * width + xs[valid]).astype(np.int32)
            polarity = polarity[valid]
            if not len(pixels):
                continue
            unique_pixels, positions_from_end = np.unique(pixels[::-1], return_index=True)
            latest_polarity = polarity[::-1][positions_from_end]
            canvas[unique_pixels] = np.where(
                latest_polarity[:, None] == 1,
                np.array([0, 0, 255], dtype=np.uint8),
                np.array([255, 0, 0], dtype=np.uint8),
            )
        return canvas.reshape((height, width, 3))

    @lru_cache(maxsize=64)
    def render_png(self, frame_index: int, duration_ms: int) -> bytes:
        image = self.render_array(frame_index, duration_ms / 1000)
        ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if not ok:
            raise RuntimeError("Could not encode event visualization")
        return encoded.tobytes()


class LikeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS likes (
                session_id TEXT NOT NULL,
                frame_idx INTEGER NOT NULL,
                voter_id TEXT NOT NULL,
                PRIMARY KEY (session_id, frame_idx, voter_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS likes_voter_idx ON likes(voter_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_key TEXT NOT NULL UNIQUE,
                voter_id TEXT NOT NULL UNIQUE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                claimed_at INTEGER
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS login_sessions (
                token_hash TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                expires_at INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS likes_dedup_archive (
                session_id TEXT NOT NULL,
                frame_idx INTEGER NOT NULL,
                voter_id TEXT NOT NULL,
                archived_at INTEGER NOT NULL
            )""")
            duplicates = db.execute("""SELECT session_id, frame_idx FROM likes
                GROUP BY session_id, frame_idx HAVING COUNT(*) > 1""").fetchall()
            for session_id, frame_idx in duplicates:
                votes = db.execute("""SELECT likes.rowid, likes.voter_id FROM likes
                    LEFT JOIN accounts ON likes.voter_id=accounts.voter_id
                    WHERE likes.session_id=? AND likes.frame_idx=?
                    ORDER BY (accounts.id IS NOT NULL) DESC, likes.rowid DESC""",
                    (session_id, frame_idx)).fetchall()
                for rowid, voter_id in votes[1:]:
                    db.execute("INSERT INTO likes_dedup_archive VALUES (?, ?, ?, ?)",
                               (session_id, frame_idx, voter_id, int(time.time())))
                    db.execute("DELETE FROM likes WHERE rowid=?", (rowid,))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS likes_one_per_frame ON likes(session_id, frame_idx)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout = 10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def toggle(self, session_id: str, frame_idx: int, voter_id: str) -> tuple[bool, int]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM likes WHERE session_id=? AND frame_idx=?",
                                  (session_id, frame_idx)).fetchone()
            if existing:
                db.execute("DELETE FROM likes WHERE session_id=? AND frame_idx=?",
                           (session_id, frame_idx))
            else:
                db.execute("INSERT INTO likes (session_id, frame_idx, voter_id) VALUES (?, ?, ?)",
                           (session_id, frame_idx, voter_id))
            count = db.execute("SELECT COUNT(*) FROM likes WHERE session_id=? AND frame_idx=?",
                               (session_id, frame_idx)).fetchone()[0]
            return not bool(existing), count

    def counts(self, session_id: str, voter_id: str) -> dict[int, tuple[int, bool]]:
        with self._connect() as db:
            result = db.execute("""SELECT frame_idx, COUNT(*),
                                MAX(CASE WHEN voter_id=? THEN 1 ELSE 0 END)
                                FROM likes WHERE session_id=? GROUP BY frame_idx""",
                                (voter_id, session_id)).fetchall()
        return {int(index): (int(count), bool(mine)) for index, count, mine in result}

    @staticmethod
    def _reconcile_visitor(db, voter_id: str, visitor_id: str) -> None:
        if voter_id == visitor_id or db.execute(
            "SELECT 1 FROM accounts WHERE voter_id=?", (visitor_id,)
        ).fetchone():
            return
        db.execute("""DELETE FROM likes WHERE voter_id=? AND EXISTS (
            SELECT 1 FROM likes AS account_vote
            WHERE account_vote.session_id=likes.session_id
              AND account_vote.frame_idx=likes.frame_idx
              AND account_vote.voter_id=?
        )""", (visitor_id, voter_id))
        db.execute("UPDATE likes SET voter_id=? WHERE voter_id=?", (voter_id, visitor_id))

    def reconcile_visitor(self, voter_id: str, visitor_id: str) -> None:
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM likes WHERE voter_id=? LIMIT 1", (visitor_id,)).fetchone():
                return
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_visitor(db, voter_id, visitor_id)

    def create_account(self, username: str, password: str) -> None:
        username = unicodedata.normalize("NFKC", username.strip())
        if not 2 <= len(username) <= 24 or any(not character.isprintable() for character in username):
            raise ValueError("Username must contain 2-24 printable characters")
        if not 1 <= len(password) <= 128:
            raise ValueError("Password must contain 1-128 characters")
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        with self._connect() as db:
            db.execute("""INSERT INTO accounts
                (id, username, username_key, voter_id, password_salt, password_hash)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), username, username.casefold(), str(uuid.uuid4()),
                 salt, password_hash))

    def login(self, username: str, password: str, visitor_id: str) -> tuple[str, dict] | None:
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        username_key = unicodedata.normalize("NFKC", username.strip()).casefold()
        if len(username_key) > 24 or len(password) > 128:
            return None
        with self._connect() as db:
            row = db.execute("""SELECT id, username, voter_id, password_salt, password_hash
                FROM accounts WHERE username_key=?""", (username_key,)).fetchone()
        if row is None:
            return None
        account_id, display_name, voter_id, salt, expected_hash = row
        actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        if not hmac.compare_digest(actual_hash, expected_hash):
            return None
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_visitor(db, voter_id, visitor_id)
            claimed_at = db.execute("SELECT claimed_at FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
            if claimed_at is None:
                db.execute("UPDATE accounts SET claimed_at=? WHERE id=?", (now, account_id))
            db.execute("DELETE FROM login_sessions WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO login_sessions VALUES (?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), account_id, now + 30 * 86400))
        return token, {"id": account_id, "username": display_name, "voter_id": voter_id}

    def account_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("""SELECT accounts.id, accounts.username, accounts.voter_id
                FROM login_sessions JOIN accounts ON login_sessions.account_id=accounts.id
                WHERE token_hash=? AND expires_at>?""", (token_hash, int(time.time()))).fetchone()
        if row is None:
            return None
        return {"id": row[0], "username": row[1], "voter_id": row[2]}

    def logout(self, token: str | None) -> None:
        if token:
            with self._connect() as db:
                db.execute("DELETE FROM login_sessions WHERE token_hash=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))

    def likers(self, session_id: str, frame_idx: int) -> dict:
        with self._connect() as db:
            rows = db.execute("""SELECT accounts.username FROM likes
                LEFT JOIN accounts ON likes.voter_id=accounts.voter_id
                WHERE likes.session_id=? AND likes.frame_idx=?
                ORDER BY accounts.username""", (session_id, frame_idx)).fetchall()
        users = [name for (name,) in rows if name is not None]
        return {"count": len(rows), "users": users,
                "anonymous_count": len(rows) - len(users)}
