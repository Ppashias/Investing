"""FastAPI dependencies: core access, auth, database sessions."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.config import get_secret
from jarvis.core import JarvisCore
from jarvis.db.models import User
from jarvis.logging import get_logger

log = get_logger(__name__)


def get_core(request: Request) -> JarvisCore:
    core = getattr(request.app.state, "core", None)
    if core is None:  # pragma: no cover - only if startup did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JARVIS core is not initialised",
        )
    return core


CoreDep = Annotated[JarvisCore, Depends(get_core)]


async def get_session(core: CoreDep) -> AsyncIterator[AsyncSession]:
    """One session per request.

    Rolled back on the way out if the handler left work uncommitted — the
    orchestrator commits explicitly, so anything still open at this point is a
    handler that raised.
    """
    async with core.database.session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_auth(
    core: CoreDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer token check.

    JARVIS binds to loopback, but loopback is shared with every other process
    on the machine — including a browser running arbitrary web pages. A token
    is the difference between "my agent" and "anything that can reach port
    8787", so it is on by default.

    Compared with :func:`secrets.compare_digest` to avoid leaking the token
    through response timing.
    """
    if not core.settings.require_auth:
        return

    expected = get_secret(core.settings.api_token_name)
    if not expected:
        # Fail closed. An unconfigured token with auth enabled must not mean
        # "let everyone in".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Auth is enabled but {core.settings.api_token_name} is not set. "
                "Set it, or set JARVIS_REQUIRE_AUTH=false for local development."
            ),
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, expected.reveal()):
        log.warning("auth_rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AuthDep = Annotated[None, Depends(require_auth)]


async def get_current_user(session: SessionDep) -> User:
    return await JarvisCore.ensure_default_user(session)


UserDep = Annotated[User, Depends(get_current_user)]
