"""Explicit FastCAS service identity for scoped FastResearch delivery."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def service_configuration(issuer=None, client_id=None, ingest_key=None):
    issuer = (issuer if issuer is not None else os.environ.get("FASTINSIGHT_FASTCAS_ISSUER", "")).strip()
    client_id = (client_id if client_id is not None else os.environ.get("FASTINSIGHT_FASTCAS_CLIENT_ID", "")).strip()
    secret = os.environ.get("FASTINSIGHT_FASTCAS_CLIENT_SECRET", "").strip()
    key = (ingest_key if ingest_key is not None else os.environ.get("FASTINSIGHT_INGEST_KEY", "")).strip()
    configured = any((issuer, client_id, secret))
    if configured and (not all((issuer, client_id, secret)) or key):
        raise ValueError("FastCAS 服务身份需要 issuer/client ID/client secret，且不能与 ingest key 混用")
    if not configured and not key:
        raise ValueError("FASTINSIGHT_INGEST_KEY 或完整 FastCAS 服务身份配置是必需的")
    return (issuer, client_id, secret) if configured else None


def service_token(configuration, endpoint):
    destination = urlparse(endpoint)
    if destination.scheme != "https" and not (
        destination.scheme == "http" and destination.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("服务令牌只能发送到 HTTPS 或环回开发端点")
    if destination.username or destination.password or destination.fragment:
        raise ValueError("FastResearch 端点不能包含用户信息或片段")
    # Optional dependency: legacy publishing never imports the FastCAS SDK.
    from fastcas import Configuration, FastCAS

    class NoBrowserTransactions:
        def put(self, _):
            raise RuntimeError("service identity cannot start a browser login")

        def take(self, _):
            raise RuntimeError("service identity cannot finish a browser login")

    issuer, client_id, secret = configuration
    sdk = FastCAS(Configuration(
        issuer, client_id, issuer + "/console/", secret,
        allow_loopback_http=os.environ.get("FASTINSIGHT_FASTCAS_ALLOW_LOOPBACK_HTTP") == "true",
    ), NoBrowserTransactions())
    try:
        return sdk.client_credentials(["insight:publish"])["access_token"]
    finally:
        sdk.close()
