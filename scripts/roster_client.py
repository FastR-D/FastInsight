"""Read a fresh routing snapshot from FastResearch's configuration database."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

if __package__:
    from .service_auth import service_configuration, service_token
else:
    from service_auth import service_configuration, service_token

MAX_RESPONSE_BYTES = 1024 * 1024


def research_endpoint(endpoint=None):
    value = (endpoint or os.environ.get("FASTRESEARCH_URL", "http://127.0.0.1:8787")).strip().rstrip("/")
    parsed = urlparse(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("FASTRESEARCH_URL 必须是不含账号、查询或片段的服务地址")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("分发配置凭据只能发送到 HTTPS 或环回开发端点")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward service/admin credentials elsewhere.
        return None


def request_json(endpoint, path, headers, *, method="GET", payload=None):
    url = research_endpoint(endpoint) + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "Content-Type": "application/json", **headers}, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("分发配置响应超过大小限制")
    return json.loads(raw.decode("utf-8"))


def validate_roster(roster):
    if not isinstance(roster, dict) or not isinstance(roster.get("directions"), list):
        raise ValueError("分发配置必须包含 directions 列表")
    def owner(value):
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
            raise ValueError("分发配置中的成员缺少有效姓名")
        for key in ("id", "receiver_ref", "feishu_open_id"):
            if key in value and not isinstance(value[key], str):
                raise ValueError("分发配置中的成员标识格式无效")
    for direction in roster["directions"]:
        if not isinstance(direction, dict) or not isinstance(direction.get("name"), str) or not direction["name"].strip():
            raise ValueError("分发配置中的方向缺少有效名称")
        keywords, owners = direction.get("keywords"), direction.get("owners")
        if not isinstance(keywords, list) or any(not isinstance(k, str) or not k.strip() for k in keywords):
            raise ValueError("分发配置中的关键词格式无效")
        if not isinstance(owners, list):
            raise ValueError("分发配置中的方向成员必须是列表")
        for value in owners:
            owner(value)
    if roster.get("default_owner") is not None:
        owner(roster["default_owner"])
    return roster


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get("storage") != "database" or not isinstance(value.get("revision"), str) or not value["revision"]:
        raise ValueError("服务未返回有效的数据库分发配置版本")
    validate_roster(value.get("roster"))
    return value


def fetch_roster(endpoint=None, ingest_key=None):
    endpoint = research_endpoint(endpoint)
    key = ingest_key if ingest_key is not None else os.environ.get("FASTINSIGHT_INGEST_KEY", "")
    configuration = service_configuration(ingest_key=key)
    headers = {"Authorization": "Bearer " + service_token(configuration, endpoint)} if configuration else {"X-FastInsight-Key": key}
    try:
        return validate_snapshot(request_json(endpoint, "/api/fastinsight/roster", headers))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"读取数据库分发配置失败（HTTP {error.code}），请检查 Research 服务版本和服务授权；未使用本地文件兜底") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError("无法读取有效的数据库分发配置；本次分发已停止，请检查 Research 服务") from error
