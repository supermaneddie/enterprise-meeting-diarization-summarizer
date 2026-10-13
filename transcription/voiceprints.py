from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from threading import Lock

from pydub import AudioSegment

from .db import utcnow


class VoiceprintError(RuntimeError):
    pass


RECOGNITION_STRATEGIES = {
    "centroid": ("Average / Centroid", "Compare with the normalized average of each speaker's saved samples. A stable default."),
    "best_match": ("Best Match", "Compare with every saved sample and use each speaker's highest score. Useful for varied recording conditions, but more sensitive to outlier samples."),
    "template_update": ("Template Update", "Start with each speaker's centroid and adapt a separate template using 10% of a new match when its score is at least 0.80 (or the configured threshold, if higher) and leads the next speaker by 0.05. Saved samples stay intact; adding or deleting samples resets templates for that model. Incorrect confident matches can affect future recognition."),
}


class VoiceprintStore:
    """SQLite-backed samples with a persisted FAISS centroid index per model."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.database_path = directory / "voiceprints.sqlite3"
        self._lock = Lock()

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voiceprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    job_id TEXT,
                    speaker_label TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, speaker_label, model)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_voiceprints_model_person "
                "ON voiceprints(model, person_id)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recognition_templates (
                    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    PRIMARY KEY(person_id, model)
                )
            """)

    @staticmethod
    def _imports():
        try:
            import faiss
            import numpy as np
        except ImportError as exc:
            raise VoiceprintError(
                "Voiceprint recognition requires faiss-cpu and numpy. "
                "Install requirements-diarization.txt."
            ) from exc
        return faiss, np

    def add(
        self,
        name: str,
        embedding,
        model: str,
        job_id: str | None = None,
        speaker_label: str | None = None,
    ) -> int:
        _faiss, np = self._imports()
        vector = np.asarray(embedding, dtype="float32").reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not vector.size or not np.isfinite(vector).all() or not norm:
            raise VoiceprintError("The extracted voiceprint embedding is empty")
        vector /= norm
        clean_name = name.strip()
        if not clean_name:
            raise VoiceprintError("A speaker name is required")
        now = utcnow()
        with self._lock:
            with sqlite3.connect(self.database_path) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                row = conn.execute(
                    "SELECT id FROM people WHERE name_key=?", (clean_name.casefold(),)
                ).fetchone()
                if row:
                    person_id = int(row[0])
                    conn.execute(
                        "UPDATE people SET name=?, updated_at=? WHERE id=?",
                        (clean_name, now, person_id),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT INTO people(name,name_key,created_at,updated_at) VALUES(?,?,?,?)",
                        (clean_name, clean_name.casefold(), now, now),
                    )
                    person_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO voiceprints(
                        person_id,model,dimension,embedding,job_id,speaker_label,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(job_id,speaker_label,model) DO UPDATE SET
                        person_id=excluded.person_id,
                        dimension=excluded.dimension,
                        embedding=excluded.embedding,
                        created_at=excluded.created_at
                    """,
                    (
                        person_id,
                        model,
                        int(vector.size),
                        vector.tobytes(),
                        job_id,
                        speaker_label,
                        now,
                    ),
                )
                conn.execute("DELETE FROM recognition_templates WHERE model=?", (model,))
            count = self._rebuild_index(model)
        return count

    def search(self, embedding, model: str, limit: int = 5,
               strategy: str = "centroid", threshold: float = 0.4) -> list[dict]:
        if strategy not in RECOGNITION_STRATEGIES:
            raise VoiceprintError("Unknown voiceprint recognition strategy")
        if strategy != "centroid":
            return self._search_samples(embedding, model, limit, strategy, threshold)
        faiss, np = self._imports()
        vector = np.asarray(embedding, dtype="float32").reshape(1, -1)
        norm = float(np.linalg.norm(vector))
        if not vector.size or not np.isfinite(vector).all() or not norm:
            return []
        vector /= norm
        with self._lock:
            index_path, mapping_path = self._index_paths(model)
            if not index_path.is_file() or not mapping_path.is_file():
                self._rebuild_index(model)
            if not index_path.is_file() or not mapping_path.is_file():
                return []
            index = faiss.read_index(str(index_path))
            if index.d != vector.shape[1] or not index.ntotal:
                return []
            people = json.loads(mapping_path.read_text(encoding="utf-8"))
            take = min(max(1, limit), index.ntotal)
            scores, indexes = index.search(vector, take)
        return [
            {
                "name": people[int(position)]["name"],
                "score": round(float(score), 4),
                "voiceprints": int(people[int(position)]["voiceprints"]),
            }
            for score, position in zip(scores[0], indexes[0])
            if int(position) >= 0
        ]

    def _search_samples(self, embedding, model, limit, strategy, threshold):
        _faiss, np = self._imports()
        query = np.asarray(embedding, dtype="float32").reshape(-1)
        norm = float(np.linalg.norm(query))
        if not query.size or not np.isfinite(query).all() or not norm:
            return []
        query /= norm
        with self._lock, sqlite3.connect(self.database_path) as conn:
            rows = conn.execute("""
                SELECT p.id, p.name, v.embedding FROM voiceprints v
                JOIN people p ON p.id=v.person_id
                WHERE v.model=? AND v.dimension=? ORDER BY p.id, v.id
            """, (model, int(query.size))).fetchall()
            grouped = defaultdict(list)
            names = {}
            for person_id, name, blob in rows:
                grouped[person_id].append(np.frombuffer(blob, dtype="float32"))
                names[person_id] = name
            templates = {
                row[0]: np.frombuffer(row[1], dtype="float32")
                for row in conn.execute(
                    "SELECT person_id, embedding FROM recognition_templates WHERE model=? AND dimension=?",
                    (model, int(query.size)),
                )
            }
            ranked = []
            vectors = {}
            for person_id, samples in grouped.items():
                if strategy == "best_match":
                    score = float(np.max(np.stack(samples) @ query))
                else:
                    vector = templates.get(person_id)
                    if vector is None:
                        vector = np.mean(np.stack(samples), axis=0)
                    norm = float(np.linalg.norm(vector))
                    if not norm:
                        continue
                    vector = vector / norm
                    vectors[person_id] = vector
                    score = float(vector @ query)
                ranked.append((score, person_id))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            if strategy == "template_update" and ranked:
                score, person_id = ranked[0]
                if score >= max(0.80, threshold) and (len(ranked) == 1 or score - ranked[1][0] >= 0.05):
                    updated = 0.9 * vectors[person_id] + 0.1 * query
                    updated = (updated / np.linalg.norm(updated)).astype("float32")
                    conn.execute(
                        "INSERT OR REPLACE INTO recognition_templates VALUES(?,?,?,?)",
                        (person_id, model, int(query.size), updated.tobytes()),
                    )
            return [
                {"name": names[person_id], "score": round(score, 4),
                 "voiceprints": len(grouped[person_id])}
                for score, person_id in ranked[:max(1, limit)]
            ]

    def rename(self, person_id: int, name: str) -> bool:
        name = name.strip()
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise VoiceprintError("Speaker names must be 1–100 characters and contain no controls")
        with self._lock, sqlite3.connect(self.database_path) as conn:
            if conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone() is None:
                return False
            if conn.execute("SELECT 1 FROM people WHERE name_key=? AND id<>?", (name.casefold(), person_id)).fetchone():
                raise VoiceprintError("Another speaker already has that name")
            for (model,) in conn.execute("SELECT DISTINCT model FROM voiceprints WHERE person_id=?", (person_id,)):
                for path in self._index_paths(model):
                    path.unlink(missing_ok=True)
            conn.execute("UPDATE people SET name=?, name_key=?, updated_at=? WHERE id=?",
                         (name, name.casefold(), utcnow(), person_id))
        return True

    def has_model(self, model: str) -> bool:
        with sqlite3.connect(self.database_path) as conn:
            return conn.execute(
                "SELECT 1 FROM voiceprints WHERE model=? LIMIT 1", (model,)
            ).fetchone() is not None

    def list_people(self) -> list[dict]:
        """List sample metadata without loading model dependencies or embeddings."""
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT p.id AS person_id, p.name, v.id, v.model, v.dimension,
                       v.job_id, v.speaker_label, v.created_at
                FROM people p JOIN voiceprints v ON v.person_id=p.id
                ORDER BY p.name_key, p.id, v.created_at DESC, v.id DESC
            """).fetchall()
        people = {}
        for row in rows:
            sample = dict(row)
            person_id = sample.pop("person_id")
            name = sample.pop("name")
            people.setdefault(person_id, {"id": person_id, "name": name, "samples": []})[
                "samples"
            ].append(sample)
        return list(people.values())

    def delete(self, sample_id: int) -> bool:
        with self._lock:
            with sqlite3.connect(self.database_path) as conn:
                row = conn.execute(
                    "SELECT model, person_id FROM voiceprints WHERE id=?", (sample_id,)
                ).fetchone()
                if row is None:
                    return False
                # Invalidate before committing so recognition never uses deleted samples.
                # The next search rebuilds the index from the remaining database rows.
                for path in self._index_paths(row[0]):
                    path.unlink(missing_ok=True)
                conn.execute("DELETE FROM voiceprints WHERE id=?", (sample_id,))
                conn.execute("DELETE FROM recognition_templates WHERE model=?", (row[0],))
                conn.execute(
                    "DELETE FROM people WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM voiceprints WHERE person_id=?)", (row[1], row[1])
                )
        return True

    def _rebuild_index(self, model: str) -> int:
        faiss, np = self._imports()
        with sqlite3.connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, v.dimension, v.embedding
                FROM voiceprints v JOIN people p ON p.id=v.person_id
                WHERE v.model=? ORDER BY p.id, v.id
                """,
                (model,),
            ).fetchall()
        grouped = defaultdict(list)
        names = {}
        dimension = None
        for person_id, name, item_dimension, blob in rows:
            dimension = dimension or int(item_dimension)
            if int(item_dimension) != dimension:
                continue
            grouped[int(person_id)].append(np.frombuffer(blob, dtype="float32").copy())
            names[int(person_id)] = str(name)
        index_path, mapping_path = self._index_paths(model)
        if not grouped or dimension is None:
            index_path.unlink(missing_ok=True)
            mapping_path.unlink(missing_ok=True)
            return 0
        people = []
        centroids = []
        for person_id, samples in grouped.items():
            centroid = np.mean(np.stack(samples), axis=0).astype("float32")
            norm = float(np.linalg.norm(centroid))
            if not norm:
                continue
            centroids.append(centroid / norm)
            people.append(
                {"id": person_id, "name": names[person_id], "voiceprints": len(samples)}
            )
        if not centroids:
            index_path.unlink(missing_ok=True)
            mapping_path.unlink(missing_ok=True)
            return 0
        index = faiss.IndexFlatIP(dimension)
        index.add(np.stack(centroids).astype("float32"))
        temporary_index = index_path.with_suffix(".faiss.tmp")
        temporary_mapping = mapping_path.with_suffix(".json.tmp")
        faiss.write_index(index, str(temporary_index))
        temporary_mapping.write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
        temporary_index.replace(index_path)
        temporary_mapping.replace(mapping_path)
        return len(people)

    def _index_paths(self, model: str) -> tuple[Path, Path]:
        key = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
        return self.directory / f"centroids-{key}.faiss", self.directory / f"centroids-{key}.json"


class VoiceprintEngine:
    def __init__(self, store: VoiceprintStore):
        self.store = store
        self._models = {}
        self._lock = Lock()

    def embedding(self, audio_path: Path, model_name: str, device: str):
        # Remote configuration applies to diarization; voiceprints stay local.
        from .devices import parse_remote_device
        if parse_remote_device(device) is not None:
            device = "cpu"
        try:
            import numpy as np
            import torch
            try:
                from speechbrain.inference.speaker import EncoderClassifier
            except ImportError:
                from speechbrain.pretrained import EncoderClassifier
        except ImportError as exc:
            raise VoiceprintError(
                "Voiceprint extraction requires SpeechBrain, PyTorch, and numpy. "
                "Install requirements-diarization.txt."
            ) from exc

        with self._lock:
            key = (model_name, device)
            classifier = self._models.get(key)
            if classifier is None:
                try:
                    classifier = EncoderClassifier.from_hparams(
                        source=model_name,
                        savedir=str(self.store.directory / "models" / hashlib.sha256(model_name.encode()).hexdigest()[:16]),
                        run_opts={"device": device},
                    )
                except Exception as exc:
                    raise VoiceprintError(f"Could not load SpeechBrain speaker model: {exc}") from exc
                self._models[key] = classifier
            try:
                # TorchAudio 2.9+ delegates load() to TorchCodec. Decode through
                # the application's existing FFmpeg/pydub path so voiceprint
                # extraction is independent of TorchCodec binary compatibility.
                audio = (
                    AudioSegment.from_file(audio_path)
                    .set_channels(1)
                    .set_frame_rate(16000)
                )
                samples = np.array(
                    audio.get_array_of_samples(), dtype=np.float32, copy=True
                )
                samples /= float(1 << (8 * audio.sample_width - 1))
                waveform = torch.from_numpy(samples).unsqueeze(0)
                with torch.inference_mode():
                    result = classifier.encode_batch(waveform.to(device))
                return result.squeeze().detach().cpu().float().numpy()
            except Exception as exc:
                raise VoiceprintError(f"Could not extract a voiceprint from the audio: {exc}") from exc

    def recognize(
        self, audio_path: Path, model_name: str, device: str, limit: int = 5,
        strategy: str = "centroid", threshold: float = 0.4,
    ) -> list[dict]:
        if not self.store.has_model(model_name):
            return []
        return self.store.search(
            self.embedding(audio_path, model_name, device), model_name, limit=limit,
            strategy=strategy, threshold=threshold,
        )

    def enroll(
        self,
        name: str,
        audio_path: Path,
        model_name: str,
        device: str,
        job_id: str | None = None,
        speaker_label: str | None = None,
    ) -> int:
        return self.store.add(
            name,
            self.embedding(audio_path, model_name, device),
            model_name,
            job_id,
            speaker_label,
        )
