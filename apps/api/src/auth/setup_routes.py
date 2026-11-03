"""Token-authorized owner initialization, separate from public registration."""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from tinypods_api.auth.contracts import OwnerSetupRequest, RegistrationResponse, SetupStatusResponse
from tinypods_api.auth.dependencies import CSRF_HEADER, LOGIN_CSRF_METADATA, check_origin
from tinypods_api.http.dependencies import get_database
from tinypods_api.http.errors import error_response
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.auth import Credentials, LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.db.models import Installation

router = APIRouter(prefix="/setup", tags=["Setup"], route_class=RateLimitedRoute)


def setup_database(
    request: Request, token: Annotated[str | None, Header(alias="x-setup-token")] = None
) -> Database:
    """Require the configured operator secret before accessing setup state."""
    configured = request.app.state.settings.setup_token
    if configured is None:
        raise HTTPException(404)
    if not secrets.compare_digest((token or "").encode(), configured.get_secret_value().encode()):
        raise HTTPException(403)
    return get_database(request)


SetupDatabase = Annotated[Database, Depends(setup_database)]


@router.get(
    "",
    operation_id=build_operation_id("setup", "status", "get"),
    summary="Check owner setup access",
    response_model=SetupStatusResponse,
)
async def status(database: SetupDatabase) -> SetupStatusResponse:
    """Allow the setup form only with a valid token and an uninitialized installation."""
    async with database.session() as session:
        installation = await session.get(Installation, 1)
        if installation is None:
            raise HTTPException(503)
        if installation.owner_id is not None:
            raise HTTPException(409)
    return SetupStatusResponse(available=True)


@router.post(
    "",
    operation_id=build_operation_id("setup", "owner", "create"),
    summary="Initialize the owner account",
    status_code=201,
    response_model=RegistrationResponse,
    openapi_extra={
        **EndpointRatePolicy(name="setup", limit=10).openapi_extra(),
        **LOGIN_CSRF_METADATA,
    },
)
async def create_owner(
    body: OwnerSetupRequest, request: Request, database: SetupDatabase
) -> RegistrationResponse | JSONResponse:
    """Create the owner once with an operator token, email and matching passwords.

    The installation row lock and account creation share one transaction. Setup
    cannot create another owner after initialization, even with the original token.
    """
    check_origin(request)
    if request.headers.get(CSRF_HEADER) != "login":
        raise HTTPException(403)
    credentials = Credentials(username=str(body.email), password=body.password)
    try:
        user_id = await LocalPasswordProvider(database).bootstrap(credentials)
    except IntegrityError:
        return error_response(
            request, 409, "setup_conflict", "Cannot create the setup account with these details."
        )
    return RegistrationResponse.model_validate(
        {
            "user": {
                "id": user_id,
                "username": credentials.username,
                "is_owner": True,
            }
        }
    )
