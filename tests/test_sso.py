"""Tenant-isolated OIDC SSO configuration and protocol tests."""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import fakeredis
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from jose import jwt

from src.api.main import create_api_app
from src.api.routers import sso
from src.gateway.auth.users import UserStore


@pytest.fixture()
def sso_client(monkeypatch):
    monkeypatch.setenv("WCP_SSO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WCP_PUBLIC_BASE_URL", "https://console.example.test")
    client = TestClient(create_api_app(fakeredis.FakeStrictRedis(decode_responses=True),
                                       enable_graphql=False))
    yield client
    client.close()


def _signup(client, email="owner@example.com"):
    response = client.post("/auth/signup", json={"org_name": "Acme",
        "email": email, "password": "correct-horse-battery"})
    assert response.status_code == 200, response.text
    return response.json()


def _enterprise(client, session):
    client.app.state.services.users.set_plan(session["org_id"], "enterprise")
    return {"Authorization": f"Bearer {session['access_token']}"}


def _config_body(**overrides):
    return {"issuer": "https://idp.example.test/tenant", "client_id": "wcp-client",
            "client_secret": "do-not-store-in-plaintext", "allowed_domains": ["EXAMPLE.COM"],
            "jit_provisioning": False, "default_role": "viewer", **overrides}


def test_tenant_oidc_config_is_encrypted_admin_only_and_enterprise_only(sso_client, monkeypatch):
    owner = _signup(sso_client)
    headers = _enterprise(sso_client, owner)
    configured = sso_client.put("/api/v1/sso/oidc", headers=headers, json=_config_body())
    assert configured.status_code == 200, configured.text
    payload = configured.json()
    assert payload["allowed_domains"] == ["example.com"]
    assert payload["client_secret_configured"] is True
    assert "client_secret" not in payload and "client_secret_enc" not in payload
    assert payload["login_path"] == f"/api/auth/oidc/{owner['org_id']}/login"

    redis = sso_client.app.state.services.redis
    stored = redis.get(sso.CONFIG_KEY.format(tenant=owner["org_id"]))
    assert "do-not-store-in-plaintext" not in stored
    record = json.loads(stored)
    assert Fernet(os.environ["WCP_SSO_ENCRYPTION_KEY"].encode()).decrypt(
        record["client_secret_enc"].encode()).decode() == "do-not-store-in-plaintext"

    fetched = sso_client.get("/api/v1/sso/oidc", headers=headers)
    assert fetched.status_code == 200
    assert "do-not-store-in-plaintext" not in fetched.text
    assert "client_secret_enc" not in fetched.text
    events = sso_client.get("/api/v1/sso/audit", headers=headers)
    assert events.status_code == 200
    assert events.json()["items"][0]["event"] == "sso.oidc.configured"

    other = _signup(sso_client, "other@example.net")
    other_headers = _enterprise(sso_client, other)
    assert sso_client.get("/api/v1/sso/oidc", headers=other_headers).status_code == 404
    assert sso_client.delete("/api/v1/sso/oidc", headers=other_headers).status_code == 204
    assert sso_client.get("/api/v1/sso/oidc", headers=headers).status_code == 200
    assert sso_client.delete("/api/v1/sso/oidc", headers=headers).status_code == 204
    assert sso_client.get("/api/v1/sso/oidc", headers=headers).status_code == 404
    audit = sso_client.get("/api/v1/sso/audit", headers=headers).json()["items"]
    assert audit[0]["event"] == "sso.oidc.disabled"


def test_oidc_config_requires_enterprise_and_rejects_privileged_default_roles(sso_client):
    owner = _signup(sso_client)
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    response = sso_client.put("/api/v1/sso/oidc", headers=headers, json=_config_body())
    assert response.status_code == 403
    _enterprise(sso_client, owner)
    bad = sso_client.put("/api/v1/sso/oidc", headers=headers,
                         json=_config_body(default_role="admin"))
    assert bad.status_code == 422
    bad_issuer = sso_client.put("/api/v1/sso/oidc", headers=headers,
        json=_config_body(issuer="http://127.0.0.1:8080"))
    assert bad_issuer.status_code == 422
    bad_domain = sso_client.put("/api/v1/sso/oidc", headers=headers,
        json=_config_body(allowed_domains=["*.example.com"]))
    assert bad_domain.status_code == 422


def test_oidc_admin_routes_require_owner_or_admin(sso_client):
    owner = _signup(sso_client)
    tenant = owner["org_id"]
    users: UserStore = sso_client.app.state.services.users
    member = users.create_user("member@example.com", "member-password-123", tenant, "member")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    # Mint an access token for the member through the existing session primitive.
    from src.gateway.auth.tokens import issue_session
    member_token = issue_session(member.id, tenant, member.role)["access_token"]
    denied = sso_client.put("/api/v1/sso/oidc",
        headers={"Authorization": f"Bearer {member_token}"}, json=_config_body())
    assert denied.status_code == 403
    assert sso_client.get("/api/v1/sso/audit", headers=headers).status_code == 200


def _b64int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    public = private.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "test-key-1", "use": "sig", "alg": "RS256",
           "n": _b64int(public.n), "e": _b64int(public.e)}
    return private_pem, jwk


def test_oidc_login_uses_state_nonce_and_pkce_then_provisions_low_privilege_user(
        sso_client, monkeypatch):
    owner = _signup(sso_client)
    headers = _enterprise(sso_client, owner)
    config_response = sso_client.put("/api/v1/sso/oidc", headers=headers,
                                     json=_config_body(jit_provisioning=True))
    assert config_response.status_code == 200, config_response.text
    record = sso_client.app.state.services.redis.get(
        sso.CONFIG_KEY.format(tenant=owner["org_id"]))
    record = json.loads(record)
    private_pem, jwk = _rsa_pair()
    metadata = {"issuer": record["issuer"],
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/keys", "_jwks": [jwk]}

    async def fake_metadata(_request, _tenant, _config, **_kwargs):
        return metadata

    monkeypatch.setattr(sso, "_metadata", fake_metadata)
    login = sso_client.get(config_response.json()["login_path"], follow_redirects=False)
    assert login.status_code == 302
    assert login.headers["cache-control"] == "no-store"
    cookie_header = login.headers["set-cookie"]
    assert "HttpOnly" in cookie_header and "Secure" in cookie_header and "SameSite=lax" in cookie_header
    auth_url = urlsplit(login.headers["location"])
    auth_params = parse_qs(auth_url.query)
    state = auth_params["state"][0]
    assert auth_params["nonce"][0]
    assert auth_params["code_challenge_method"] == ["S256"]
    assert len(auth_params["code_challenge"][0]) == 43
    state_key = sso.STATE_KEY.format(digest=__import__("hashlib").sha256(state.encode()).hexdigest())
    transaction = json.loads(sso_client.app.state.services.redis.get(state_key))
    assert transaction["nonce"] == auth_params["nonce"][0]

    claims = {"iss": record["issuer"], "sub": "directory-user-42",
              "aud": record["client_id"], "iat": int(time.time()),
              "exp": int(time.time()) + 300, "nonce": transaction["nonce"],
              "email": "new.user@example.com", "email_verified": True,
              "name": "New User"}
    id_token = jwt.encode(claims, private_pem, algorithm="RS256",
                          headers={"kid": "test-key-1"})

    async def fake_http_json(url, *, method="GET", data=None, auth=None):
        assert url == metadata["token_endpoint"] and method == "POST"
        assert data["code_verifier"] == transaction["verifier"]
        assert auth is not None
        return {"id_token": id_token}

    monkeypatch.setattr(sso, "_http_json", fake_http_json)
    callback = sso_client.get("/api/auth/oidc/callback", params={"state": state, "code": "one-time-code"},
                              headers={"Cookie": f"{sso.STATE_COOKIE}={state}"},
                              follow_redirects=False)
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "https://console.example.test/app"
    session_cookie = callback.headers.get_list("set-cookie")
    assert any("wcp_session=" in value and "HttpOnly" in value for value in session_cookie)
    created = sso_client.app.state.services.users.get_by_email("new.user@example.com")
    assert created is not None and created.tenant == owner["org_id"]
    assert created.role == "viewer" and created.oauth_id == "directory-user-42"
    audit = sso_client.get("/api/v1/sso/audit", headers=headers).json()["items"]
    assert audit[0]["event"] == "sso.oidc.login.succeeded"
    assert "email" not in audit[0]
    replay = sso_client.get("/api/auth/oidc/callback",
                            params={"state": state, "code": "replay"},
                            headers={"Cookie": f"{sso.STATE_COOKIE}={state}"})
    assert replay.status_code == 400


def test_oidc_state_mismatch_and_bad_id_token_are_rejected(sso_client, monkeypatch):
    owner = _signup(sso_client)
    _enterprise(sso_client, owner)
    configured = sso_client.put("/api/v1/sso/oidc", headers={
        "Authorization": f"Bearer {owner['access_token']}"}, json=_config_body())
    record = json.loads(sso_client.app.state.services.redis.get(
        sso.CONFIG_KEY.format(tenant=owner["org_id"])))
    private_pem, jwk = _rsa_pair()
    metadata = {"issuer": record["issuer"],
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/keys", "_jwks": [jwk]}

    async def fake_metadata(*_args, **_kwargs):
        return metadata

    monkeypatch.setattr(sso, "_metadata", fake_metadata)
    login = sso_client.get(configured.json()["login_path"], follow_redirects=False)
    params = parse_qs(urlsplit(login.headers["location"]).query)
    state = params["state"][0]
    mismatch = sso_client.get("/api/auth/oidc/callback", params={"state": "wrong", "code": "x"},
                              headers={"Cookie": f"{sso.STATE_COOKIE}={state}"})
    assert mismatch.status_code == 400
    transaction_key = sso.STATE_KEY.format(digest=__import__("hashlib").sha256(state.encode()).hexdigest())
    transaction = json.loads(sso_client.app.state.services.redis.get(transaction_key))
    claims = {"iss": record["issuer"], "sub": "directory-user-43",
              "aud": record["client_id"], "iat": int(time.time()),
              "exp": int(time.time()) + 300, "nonce": "wrong-nonce",
              "email": "bad@example.com", "email_verified": True}
    token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": jwk["kid"]})

    async def fake_http_json(*_args, **_kwargs):
        return {"id_token": token}

    monkeypatch.setattr(sso, "_http_json", fake_http_json)
    rejected = sso_client.get("/api/auth/oidc/callback", params={"state": state, "code": "x"},
                              headers={"Cookie": f"{sso.STATE_COOKIE}={state}"})
    assert rejected.status_code == 401
    assert "organization administrator" in rejected.text
    assert sso_client.app.state.services.users.get_by_email("bad@example.com") is None
    assert sso_client.app.state.services.redis.get(transaction_key) is None
    assert transaction["tenant"] == owner["org_id"]


def test_oidc_callback_rejects_disallowed_domain_and_disabled_jit(sso_client, monkeypatch):
    owner = _signup(sso_client)
    headers = _enterprise(sso_client, owner)
    configured = sso_client.put("/api/v1/sso/oidc", headers=headers,
                                json=_config_body(jit_provisioning=False))
    assert configured.status_code == 200, configured.text
    record = json.loads(sso_client.app.state.services.redis.get(
        sso.CONFIG_KEY.format(tenant=owner["org_id"])))
    private_pem, jwk = _rsa_pair()
    metadata = {"issuer": record["issuer"],
        "authorization_endpoint": "https://idp.example.test/authorize",
        "token_endpoint": "https://idp.example.test/token",
        "jwks_uri": "https://idp.example.test/keys", "_jwks": [jwk]}
    monkeypatch.setattr(sso, "_metadata", _fake_metadata(metadata))
    token_holder = {"token": ""}

    async def fake_http_json(*_args, **_kwargs):
        return {"id_token": token_holder["token"]}

    monkeypatch.setattr(sso, "_http_json", fake_http_json)

    def attempt(email, subject):
        login = sso_client.get(configured.json()["login_path"], follow_redirects=False)
        params = parse_qs(urlsplit(login.headers["location"]).query)
        state = params["state"][0]
        digest = __import__("hashlib").sha256(state.encode()).hexdigest()
        transaction = json.loads(sso_client.app.state.services.redis.get(
            sso.STATE_KEY.format(digest=digest)))
        claims = {"iss": record["issuer"], "sub": subject,
                  "aud": record["client_id"], "iat": int(time.time()),
                  "exp": int(time.time()) + 300, "nonce": transaction["nonce"],
                  "email": email, "email_verified": True}
        token_holder["token"] = jwt.encode(
            claims, private_pem, algorithm="RS256", headers={"kid": jwk["kid"]})
        return sso_client.get(
            "/api/auth/oidc/callback", params={"state": state, "code": "one-time-code"},
            headers={"Cookie": f"{sso.STATE_COOKIE}={state}"},
            follow_redirects=False)

    disallowed = attempt("person@outside.example", "subject-outside-domain")
    assert disallowed.status_code == 401
    assert "outside.example" not in disallowed.text
    assert sso_client.app.state.services.users.get_by_email(
        "person@outside.example") is None

    jit_disabled = attempt("new.person@example.com", "subject-jit-disabled")
    assert jit_disabled.status_code == 401
    assert "JIT" not in jit_disabled.text
    assert sso_client.app.state.services.users.get_by_email(
        "new.person@example.com") is None
    failures = sso_client.get("/api/v1/sso/audit", headers=headers).json()["items"]
    assert [item["result"] for item in failures[:2]] == ["validation", "validation"]
    assert all("email" not in item for item in failures[:2])


def _fake_metadata(metadata):
    async def _return(*_args, **_kwargs):
        return metadata
    return _return


def test_sso_audit_history_is_bounded_and_expires(sso_client):
    redis = sso_client.app.state.services.redis
    tenant = "0123456789"
    for index in range(sso.AUDIT_LIMIT + 5):
        sso._audit(redis, tenant, f"test.event.{index}", user_id=str(index),
                   result="success")

    key = sso.AUDIT_KEY.format(tenant=tenant)
    entries = [json.loads(raw) for raw in redis.lrange(key, 0, -1)]
    assert len(entries) == sso.AUDIT_LIMIT
    assert entries[0]["event"] == f"test.event.{sso.AUDIT_LIMIT + 4}"
    assert entries[-1]["event"] == "test.event.5"
    assert 0 < redis.ttl(key) <= 365 * 24 * 60 * 60


def test_oidc_endpoint_validation_blocks_external_host_and_private_resolution(monkeypatch):
    with pytest.raises(sso._OIDCFailure):
        sso._validate_endpoint("https://169.254.169.254/latest/meta-data", "idp.example.test")
    monkeypatch.setattr(sso.socket, "getaddrinfo", lambda *_a, **_kw: [
        (None, None, None, None, ("10.0.0.3", 443))])
    with pytest.raises(sso._OIDCFailure, match="non-public"):
        sso._resolve_public("idp.example.test")


def test_oauth_identity_link_is_unique_and_tenant_bound():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    users = UserStore(redis)
    org = users.create_org("Directory test")
    first = users.create_user("first@example.com", "password-one", org.id)
    second = users.create_user("second@example.com", "password-two", org.id)
    linked = users.link_oauth_identity(first.id, "oidc:" + org.id, "subject-1")
    assert linked.oauth_provider == "oidc:" + org.id
    assert users.get_by_oauth("oidc:" + org.id, "subject-1").id == first.id
    with pytest.raises(ValueError, match="already linked"):
        users.link_oauth_identity(second.id, "oidc:" + org.id, "subject-1")
    assert users.get_by_oauth("oidc:" + org.id, "subject-1").id == first.id


def test_oidc_configuration_fails_closed_without_encryption_key(sso_client, monkeypatch):
    owner = _signup(sso_client)
    headers = _enterprise(sso_client, owner)
    monkeypatch.delenv("WCP_SSO_ENCRYPTION_KEY")
    response = sso_client.put("/api/v1/sso/oidc", headers=headers, json=_config_body())
    assert response.status_code == 503
    assert "WCP_SSO_ENCRYPTION_KEY" in response.json()["detail"]


def test_oidc_http_json_is_bounded_and_does_not_follow_redirects(monkeypatch):
    import httpx

    monkeypatch.setattr(sso, "_resolve_public", lambda _host: None)
    original = httpx.AsyncClient

    def use_transport(handler):
        transport = httpx.MockTransport(handler)
        return lambda **kwargs: original(transport=transport, **kwargs)

    monkeypatch.setattr(sso.httpx, "AsyncClient", use_transport(
        lambda request: httpx.Response(200, json={"issuer": "https://idp.example.test"})))
    import asyncio
    assert asyncio.run(sso._http_json("https://idp.example.test/.well-known/openid-configuration"))["issuer"] == "https://idp.example.test"

    monkeypatch.setattr(sso.httpx, "AsyncClient", use_transport(
        lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/"})))
    with pytest.raises(sso._OIDCFailure, match="invalid response"):
        asyncio.run(sso._http_json("https://idp.example.test/.well-known/openid-configuration"))

    monkeypatch.setattr(sso.httpx, "AsyncClient", use_transport(
        lambda request: httpx.Response(200, content=b"x" * 1_000_001)))
    with pytest.raises(sso._OIDCFailure, match="too large"):
        asyncio.run(sso._http_json("https://idp.example.test/.well-known/openid-configuration"))
