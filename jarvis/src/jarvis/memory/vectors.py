"""Vector storage and similarity search.

## Why vectors live in SQLite (§21)

The brief asks for reasoning rather than a choice, so: **vectors are stored as
``float32`` blobs in the existing SQLite database and searched by exhaustive
cosine similarity in NumPy.** No second database, no extension to load, no new
service to run.

The candidates, and why they lost:

* **A dedicated vector database** (Qdrant, Weaviate, Chroma in server mode) is
  a second process to install, run, back up, and keep version-compatible, in a
  system whose entire premise is local-first and single-user. §21 asks not to
  introduce one unnecessarily.
* **LanceDB** — the Phase 0 audit's recommendation — is embedded rather than a
  server, so that objection does not apply. It lost on a different one: it
  would make the vectors a *second store*, which means memory writes stop being
  transactional. A memory row committed while its vector write fails leaves a
  memory that exists and cannot be found, and reconciling two stores is real,
  permanent complexity. Keeping vectors in the same transaction as the row they
  describe removes an entire class of bug. LanceDB remains the right answer at
  a scale this system will not reach for years, which is what
  :class:`VectorIndex` exists for.
* **``sqlite-vec``** keeps one store and adds real ANN indexing. It needs a
  loadable extension present and matching the Python build, and the Phase 0
  audit already flagged it as pre-1.0 with slowing releases. It is worth
  revisiting when it stabilises; it would slot in behind :class:`VectorIndex`.

The engineering case for brute force is the arithmetic. Cosine similarity over
L2-normalised vectors is one matrix-vector product. At 1536 dimensions, 10,000
memories is a 61 MB ``float32`` matrix and roughly 15 million multiply-adds —
single-digit milliseconds in NumPy, and measured rather than assumed in the
Phase 2 performance notes. A personal memory system reaching 10,000 memories
would be storing one new memory every hour for a year. ANN indexing solves a
problem this workload does not have, and pays for it in index maintenance,
recall loss, and a dependency.

What makes this defensible rather than merely convenient is that it is behind
an interface. :class:`VectorIndex` has three methods. When the row count does
start to matter, the replacement is one class, and the schema — which already
records the embedding model and dimension per row — does not change.

## Storage format

Little-endian ``float32``, L2-normalised at write time with the norm stored
alongside. Normalising once at write turns every subsequent cosine similarity
into a plain dot product, and ``float32`` halves the memory bandwidth of the
scan for a precision loss far below the noise floor of the embeddings.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.db.models import Embedding, EmbeddingOwner
from jarvis.logging import get_logger

log = get_logger(__name__)

DTYPE = "<f4"  # little-endian float32, explicit so files are portable


def pack_vector(values: Sequence[float]) -> tuple[bytes, float, int]:
    """Normalise, serialise. Returns ``(blob, original_norm, dim)``.

    A zero vector is stored as-is with norm 0; it can never match anything,
    which is the correct behaviour for text that vectorised to nothing.
    """
    array = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    if norm > 0:
        array = array / norm
    return array.astype(DTYPE).tobytes(), norm, int(array.shape[0])


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=DTYPE)


@dataclass(frozen=True, slots=True)
class VectorHit:
    owner_id: str
    score: float


class VectorIndex(abc.ABC):
    """Three operations. Everything else is the caller's business."""

    @abc.abstractmethod
    async def upsert(
        self, *, owner_kind: EmbeddingOwner, owner_id: str, model: str,
        vector: Sequence[float],
    ) -> None: ...

    @abc.abstractmethod
    async def delete(self, *, owner_kind: EmbeddingOwner, owner_id: str) -> None: ...

    @abc.abstractmethod
    async def search(
        self, *, owner_kind: EmbeddingOwner, model: str, query: Sequence[float],
        top_k: int = 20, owner_ids: Iterable[str] | None = None,
        min_score: float = 0.0,
    ) -> list[VectorHit]: ...


class SqliteVectorIndex(VectorIndex):
    """Exhaustive cosine search over the ``embeddings`` table.

    Bound to a session, so writes join whatever transaction the caller is in —
    which is the whole point of keeping vectors in the primary store.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, *, owner_kind: EmbeddingOwner, owner_id: str, model: str,
        vector: Sequence[float],
    ) -> None:
        blob, norm, dim = pack_vector(vector)
        existing = (
            await self.session.execute(
                select(Embedding).where(
                    Embedding.owner_kind == owner_kind,
                    Embedding.owner_id == owner_id,
                    Embedding.model == model,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.vector = blob
            existing.norm = norm
            existing.dim = dim
        else:
            self.session.add(
                Embedding(
                    owner_kind=owner_kind, owner_id=owner_id, model=model,
                    dim=dim, vector=blob, norm=norm,
                )
            )
        await self.session.flush()

    async def delete(self, *, owner_kind: EmbeddingOwner, owner_id: str) -> None:
        await self.session.execute(
            sql_delete(Embedding).where(
                Embedding.owner_kind == owner_kind, Embedding.owner_id == owner_id
            )
        )

    async def search(
        self, *, owner_kind: EmbeddingOwner, model: str, query: Sequence[float],
        top_k: int = 20, owner_ids: Iterable[str] | None = None,
        min_score: float = 0.0,
    ) -> list[VectorHit]:
        stmt = select(Embedding.owner_id, Embedding.vector).where(
            Embedding.owner_kind == owner_kind, Embedding.model == model
        )
        if owner_ids is not None:
            ids = list(owner_ids)
            if not ids:
                # An empty pre-filter means "no candidates", not "no filter".
                # Getting this wrong is how a structured filter silently
                # becomes a full scan; Phase 1 shipped that exact bug once.
                return []
            stmt = stmt.where(Embedding.owner_id.in_(ids))

        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return []

        query_vec = np.asarray(query, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm == 0:
            return []
        query_vec = query_vec / query_norm

        # Rows with a different dimension belong to another model that happens
        # to share a name; skip rather than crash on the stack.
        dim = query_vec.shape[0]
        usable = [(owner_id, blob) for owner_id, blob in rows if len(blob) == dim * 4]
        if len(usable) != len(rows):
            log.warning(
                "vector_dim_mismatch_skipped",
                skipped=len(rows) - len(usable), model=model, expected_dim=dim,
            )
        if not usable:
            return []

        matrix = np.frombuffer(
            b"".join(blob for _, blob in usable), dtype=DTYPE
        ).reshape(len(usable), dim)
        # Both sides normalised, so the dot product *is* the cosine.
        scores = matrix @ query_vec

        k = min(top_k, len(usable))
        # argpartition is O(n) against argsort's O(n log n); the ordering of
        # the top-k slice is then fixed by a sort over k elements.
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]

        return [
            VectorHit(owner_id=usable[i][0], score=float(scores[i]))
            for i in top
            if float(scores[i]) >= min_score
        ]

    async def count(self, *, owner_kind: EmbeddingOwner, model: str) -> int:
        from sqlalchemy import func

        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Embedding)
                    .where(
                        Embedding.owner_kind == owner_kind, Embedding.model == model
                    )
                )
            ).scalar_one()
        )
