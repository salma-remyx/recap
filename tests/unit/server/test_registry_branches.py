import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import AnyUrl

from recap.server.registry import router, settings
from recap.types import IntType, StringType, StructType, to_dict

app = FastAPI()
app.include_router(router)
client = TestClient(app)

RECAP_JSON = {"Content-Type": "application/x-recap+json"}


@pytest.fixture(autouse=True)
def set_env_variable(tmp_path):
    settings.registry_storage_url = AnyUrl(tmp_path.as_uri())


def _post(name, type_):
    return client.post(f"/registry/{name}", json=to_dict(type_), headers=RECAP_JSON)


def test_branch_commit_merge_publishes_atomically():
    name = "orders"
    base = StructType(fields=[IntType(bits=64)])
    evolved = StructType(fields=[IntType(bits=64), StringType(bytes_=8)])

    # Publish version 1 on the main registry.
    assert _post(name, base).text == "1"

    # Branch off the latest published version.
    assert client.post(f"/registry/{name}/branches/feature").text == "1"

    # Commit an evolved schema to the branch.
    commit_resp = client.post(
        f"/registry/{name}/branches/feature/commits",
        json=to_dict(evolved),
        headers=RECAP_JSON,
    )
    assert commit_resp.status_code == 200
    assert commit_resp.text == "1"

    # The branch edit is not yet visible on the main registry, and the
    # branch plumbing stays out of the published schema listing.
    assert client.get(f"/registry/{name}/versions").json() == [1]
    assert client.get("/registry/").json() == [name]

    # Merge publishes the tip atomically as version 2.
    merge_resp = client.post(f"/registry/{name}/branches/feature/merge")
    assert merge_resp.status_code == 200
    assert merge_resp.text == "2"

    # The main registry now reflects the merged change.
    assert client.get(f"/registry/{name}/versions").json() == [1, 2]
    latest_dict, version = client.get(f"/registry/{name}").json()
    assert version == 2
    assert latest_dict == to_dict(evolved)


def test_branching_missing_schema_is_rejected():
    response = client.post("/registry/missing/branches/feature")
    assert response.status_code == 400


def test_commit_to_missing_branch_is_not_found():
    response = client.post(
        "/registry/whatever/branches/ghost/commits",
        json=to_dict(StructType(fields=[IntType(bits=8)])),
        headers=RECAP_JSON,
    )
    assert response.status_code == 404


def test_merge_conflict_returns_409():
    name = "orders"
    ancestor = StructType(fields=[IntType(bits=32, name="id")])
    assert _post(name, ancestor).text == "1"

    # Branch off v1 and evolve "id".
    assert client.post(f"/registry/{name}/branches/feature").text == "1"
    client.post(
        f"/registry/{name}/branches/feature/commits",
        json=to_dict(StructType(fields=[IntType(bits=64, name="id")])),
        headers=RECAP_JSON,
    )

    # A concurrent publish rewrites "id" incompatibly as v2.
    assert _post(name, StructType(fields=[StringType(name="id")])).text == "2"

    merge_resp = client.post(f"/registry/{name}/branches/feature/merge")
    assert merge_resp.status_code == 409
