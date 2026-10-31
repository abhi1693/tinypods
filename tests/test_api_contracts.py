import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from pydantic import ValidationError
from tinypods_api.contracts import OperationStatus, Page, PageParams, RequestDTO
from tinypods_api.main import create_app
from tinypods_api.openapi import export_schema, render_schema
from tinypods_core.settings import Settings


class CreateItem(RequestDTO):
    name: str
    count: int


def contract_app():
    app = create_app(Settings())

    @app.post("/test/items", operation_id="createTestItem", response_model=CreateItem)
    async def create_item(item: CreateItem) -> CreateItem:
        return item

    @app.get("/test/failure", operation_id="getTestFailure", response_model=CreateItem)
    async def failure() -> CreateItem:
        raise RuntimeError("postgresql://private-password")

    @app.get("/test/invalid-response", operation_id="getInvalidResponse", response_model=CreateItem)
    async def invalid_response() -> dict[str, str]:
        return {"private": "private-password"}

    @app.get("/test/nonstandard", operation_id="getNonstandard", response_model=CreateItem)
    async def nonstandard() -> CreateItem:
        raise HTTPException(499, detail="private-password")

    @app.get("/test/denied", operation_id="getTestDenied", response_model=CreateItem)
    async def denied() -> CreateItem:
        raise HTTPException(401, detail="private-password", headers={"WWW-Authenticate": "Bearer"})

    return app


def test_errors_are_typed_correlated_and_do_not_echo_inputs():
    with TestClient(contract_app(), raise_server_exceptions=False) as client:
        for response, code in [
            (client.get("/missing?token=private-password"), "http_404"),
            (client.put("/healthz"), "http_405"),
            (client.get("/test/failure"), "internal_error"),
            (client.get("/test/invalid-response"), "internal_error"),
            (client.get("/test/nonstandard"), "http_499"),
            (client.get("/test/denied"), "http_401"),
            (
                client.post("/test/items", json={"name": "private-password", "count": "bad"}),
                "validation_error",
            ),
            (client.post("/test/items", content='{"password":invalid'), "validation_error"),
        ]:
            payload = response.json()
            assert payload["code"] == code
            assert str(UUID(payload["request_id"])) == response.headers["x-request-id"]
            assert "private-password" not in response.text
            assert "input" not in response.text and "ctx" not in response.text
        denied = client.get("/test/denied")
        assert denied.headers["www-authenticate"] == "Bearer"
        validation = client.post("/test/items", json={"name": "ok", "count": "bad"}).json()
        assert validation["issues"] == [{"location": ["body", "count"], "code": "int_parsing"}]
        assert client.post("/test/items", json={"name": "ok", "count": 1}).json() == {
            "name": "ok",
            "count": 1,
        }


def test_pagination_and_operation_dtos():
    assert PageParams().model_dump() == {"limit": 25, "offset": 0}
    for values in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            PageParams(**values)
    page = Page[CreateItem](items=[CreateItem(name="x", count=1)], total=1, limit=25, offset=0)
    assert page.model_dump()["items"] == [{"name": "x", "count": 1}]
    with pytest.raises(ValidationError):
        OperationStatus(operation_id="not-a-uuid", status="unknown")


def test_export_is_valid_deterministic_and_has_explicit_responses():
    application = create_app(Settings())
    first = render_schema(application)
    assert first == render_schema(create_app(Settings(service_name="Different deployment")))
    schema = json.loads(first)
    validate(schema)
    assert schema["paths"]["/readyz"]["get"]["responses"]["503"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/ReadinessResponse")
    assert schema["paths"]["/healthz"]["get"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/ErrorResponse")
    assert all(
        name not in first
        for name in ("database_url", "migration_database_url", "password", "Installation", "plugin")
    )
    assert first == Path("openapi.json").read_text()


@pytest.mark.parametrize(
    "mode", ["missing_id", "duplicate_id", "missing_model", "multiple_methods"]
)
def test_export_rejects_ambiguous_routes(mode):
    app = FastAPI()

    async def endpoint() -> dict[str, str]:
        return {"status": "alive"}

    from tinypods_api.contracts import LivenessResponse

    options = {"response_model": LivenessResponse}
    if mode != "missing_id":
        options["operation_id"] = "testOperation"
    if mode == "missing_model":
        options.pop("response_model")
    app.add_api_route(
        "/one",
        endpoint,
        methods=["GET", "POST"] if mode == "multiple_methods" else ["GET"],
        **options,
    )
    if mode == "duplicate_id":
        app.add_api_route("/two", endpoint, methods=["GET"], **options)
    with pytest.raises(ValueError):
        export_schema(app)


def test_validator_rejects_invalid_response_schema():
    schema = export_schema(create_app(Settings()))
    schema["paths"]["/healthz"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = {"type": "invalid-type"}
    with pytest.raises(OpenAPIValidationError):
        validate(schema)


def test_export_command_is_offline_and_checks_drift(tmp_path):
    output = tmp_path / "openapi.json"
    environment = dict(
        os.environ,
        TINYPODS_DATABASE_URL="invalid-secret",
        TINYPODS_API_PORT="invalid",
        TINYPODS_SERVICE_NAME="private-name",
    )
    command = ["tinypods-openapi", "--output", str(output)]
    first = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    before = output.read_bytes()
    assert subprocess.run(command, env=environment, capture_output=True, timeout=15).returncode == 0
    assert output.read_bytes() == before == Path("openapi.json").read_bytes()
    assert subprocess.run(command + ["--check"], capture_output=True, timeout=15).returncode == 0
    output.write_text("{}")
    assert subprocess.run(command + ["--check"], capture_output=True, timeout=15).returncode == 1
    output.unlink()
    assert subprocess.run(command + ["--check"], capture_output=True, timeout=15).returncode == 1
