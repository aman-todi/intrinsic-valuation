"""Account self-service: ``DELETE /api/me`` removes the caller and everything private to them.

Order (each step is safe to retry):
1. Refuse while a valuation is building (a worker owns that run; deleting it underneath the worker
   would race the build). A run waiting for confirmation is just deleted.
2. Delete the private artifacts of every run (``runs/{id}/`` in storage). The shared, user-agnostic
   cache (``models/...``, ``cached_models``, ``cached_proposals``) is kept: it holds no personal data.
3. Delete the ``users`` row; ``runs`` and ``run_events`` go with it (ON DELETE CASCADE).
4. Delete the Cognito user, so the email can no longer sign in (refresh tokens die with it; an access
   token already issued stays valid until it expires, at most an hour).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select

from app.api.routes.runs import Storage
from app.config import get_settings
from app.db.models import Run, User
from app.deps import DEV_USER_ID, CurrentUser, DbSession, forget_user
from app.schemas.run import ACTIVE_STATUSES
from app.storage import run_prefix

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me", tags=["account"])


class IdentityDeleteFailed(Exception):
    """The sign-in identity could not be removed (the app data is already gone; retrying is safe)."""


class CognitoUserAdmin:
    """Deletes a user from the Cognito user pool (needs ``cognito-idp:AdminDeleteUser``)."""

    def __init__(self, user_pool_id: str, region: str, client: Any | None = None) -> None:
        self.user_pool_id = user_pool_id
        self.region = region
        self._client = client

    def _delete_sync(self, user_id: UUID) -> None:
        if self._client is None:
            import boto3

            self._client = boto3.client("cognito-idp", region_name=self.region)
        try:
            # With email as the username attribute, Cognito's username is the ``sub``.
            self._client.admin_delete_user(UserPoolId=self.user_pool_id, Username=str(user_id))
        except self._client.exceptions.UserNotFoundException:
            pass  # already gone (e.g. a retried delete)

    async def delete_user(self, user_id: UUID) -> None:
        if not self.user_pool_id or user_id == DEV_USER_ID:
            return  # dev auth bypass / no pool configured: there is no Cognito identity
        try:
            await asyncio.to_thread(self._delete_sync, user_id)
        except Exception as exc:  # boto3 raises many error types
            raise IdentityDeleteFailed(str(exc)) from exc


def identity_admin_dep() -> CognitoUserAdmin:
    s = get_settings()
    return CognitoUserAdmin(s.COGNITO_USER_POOL_ID, s.COGNITO_REGION)


IdentityAdmin = Annotated[CognitoUserAdmin, Depends(identity_admin_dep)]


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    user: CurrentUser, session: DbSession, storage: Storage, identity: IdentityAdmin
) -> Response:
    runs = (await session.execute(select(Run.id, Run.status).where(Run.user_id == user.id))).all()
    if any(r.status in ACTIVE_STATUSES for r in runs):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A valuation is still being built. Cancel it (or let it finish), then delete your account.",
        )

    for run_id, _ in runs:
        await storage.delete_prefix(run_prefix(run_id))
    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()
    forget_user(user.id)
    log.info("deleted account %s (%d runs)", user.id, len(runs))

    try:
        await identity.delete_user(user.id)
    except IdentityDeleteFailed:
        log.exception("Cognito delete failed for %s (app data already deleted)", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your data was deleted, but your sign-in could not be removed. Please try again.",
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
