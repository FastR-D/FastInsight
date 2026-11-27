import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.transport.http_transport import route_urlopen


class HTTPWorld:
    """External academic/search/model/Research protocols; production internals stay real."""
    def __init__(self):
        self.title = "Unrelated astronomy observations"
        self.receipts = {}
        self.roster = {"directions": [{"name": "Firmware", "keywords": ["firmware"], "owners": [{"id": "a", "name": "A", "receiver_ref": "account-a"}, {"id": "b", "name": "B", "receiver_ref": "account-b"}]}], "default_owner": {"id": "a", "name": "A", "receiver_ref": "account-a"}}
        self.roster_revision = "revision-1"
        self.roster_status = 200
        self.roster_reads = 0
        self.published = []
        self.requests = []
        self.notifications = []
        self.model_entered = threading.Event()
        self.model_release = threading.Event()
        self.model_release.set()
        self.verification_entered = threading.Event()
        self.verification_release = threading.Event()
        self.verification_release.set()
        self.image_entered = threading.Event()
        self.image_release = threading.Event()
        self.image_release.set()
        self.publish_entered = threading.Event()
        self.publish_release = threading.Event()
        self.publish_release.set()
        world = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                target = self.headers.get("X-Fixture-Url", self.path)
                path = urllib.parse.urlsplit(target).path
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                world.requests.append(target)
                status, content_type = 200, "application/json"
                if path == "/open-apis/auth/v3/tenant_access_token/internal":
                    value = {"code": 0, "tenant_access_token": "fixture-token", "expire": 7200}
                elif path == "/open-apis/im/v1/messages":
                    if self.command == "POST":
                        world.notifications.append(json.loads(json.loads(body)["content"])["text"])
                    value = {"code": 0, "data": {"items": [], "has_more": False, "message_id": "om_fixture"}}
                elif "/resources/" in path:
                    world.image_entered.set()
                    if not world.image_release.wait(10):
                        self.send_error(500)
                        return
                    data = b"\x89PNG\r\n\x1a\nfixture"
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                elif path.endswith("/chat/completions"):
                    world.model_entered.set()
                    if not world.model_release.wait(10):
                        status, value = 500, {"error": "fixture model was not released"}
                    else:
                        messages = json.loads(body).get("messages", [])
                        vision = any(isinstance(item.get("content"), list) for item in messages)
                        content = json.dumps({"title": "Known firmware paper", "url": "https://doi.org/10.1000/known"}) if vision else "A fixture trend summary."
                        value = {"choices": [{"message": {"content": content}}]}
                elif path == "/api/fastinsight/roster":
                    world.roster_reads += 1
                    if self.headers.get("X-FastInsight-Key") != "fixture-key":
                        status, value = 401, {"error": "missing fixture service identity"}
                    else:
                        status = world.roster_status
                        value = {"roster": world.roster, "revision": world.roster_revision, "storage": "database"}
                elif path == "/api/insight/publish":
                    payload = json.loads(body)
                    world.published.append(payload)
                    world.publish_entered.set()
                    if not world.publish_release.wait(10):
                        self.send_error(500)
                        return
                    value = world.receipts.get(payload.get("person"), {
                        "ok": True, "deliveredTo": [payload.get("person", "A")],
                    })
                elif "serpapi.com" in target:
                    value = {"organic_results": [{"title": world.title,
                        "link": "https://papers.example/discovered", "snippet": "Search abstract"}]}
                elif "papers.example" in target:
                    content_type = "text/html"
                    value = (f'<html><head><meta name="citation_title" content="{world.title}">'
                        '<meta name="citation_doi" content="10.9999/unrelated">'
                        '<meta name="description" content="Unrelated page abstract">'
                        '</head></html>')
                elif "api.crossref.org" in target:
                    world.verification_entered.set()
                    if not world.verification_release.wait(10):
                        self.send_error(500)
                        return
                    value = {"message": {"items": []}}
                elif "api.openalex.org" in target:
                    value = {"results": []}
                elif "semanticscholar.org" in target:
                    value = {"data": []}
                elif "arxiv.org" in target:
                    content_type, value = "application/atom+xml", '<feed xmlns="http://www.w3.org/2005/Atom"/>'
                else:
                    status, value = 404, {"error": "unrecorded external URL"}
                encoded = (json.dumps(value) if content_type == "application/json" else value).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="insight-http-"))
        self.root = Path(directory)
        transport = str(Path(__file__).parent / "transport")
        self.stack.enter_context(patch.dict(os.environ, {
            "FASTINSIGHT_FIXTURE_ORIGIN": self.origin,
            "PYTHONPATH": transport + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "FASTINSIGHT_ROSTER": "/does/not/exist/obsolete.yaml", "FASTRESEARCH_URL": self.origin,
            "FASTINSIGHT_INGEST_KEY": "fixture-key", "FASTINSIGHT_ALLOW_LEGACY_INGEST_KEY": "true",
            "FASTINSIGHT_FASTCAS_ISSUER": "", "FASTINSIGHT_FASTCAS_CLIENT_ID": "",
            "FASTINSIGHT_FASTCAS_CLIENT_SECRET": "", "OPENAI_API_KEY": "fixture-key",
            "OPENAI_BASE_URL": self.origin, "SERPAPI_API_KEY": "fixture-key",
            "BING_SEARCH_API_KEY": "", "GOOGLE_SEARCH_API_KEY": "",
            "PYTHONUTF8": "1", "FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret",
            "FEISHU_BOT_OPEN_ID": "ou_fixture", "FASTINSIGHT_STATE_DIR": str(self.root),
        }))
        self.stack.enter_context(patch("urllib.request.urlopen", route_urlopen(urllib.request.urlopen, self.origin)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.model_release.set()
        self.verification_release.set()
        self.image_release.set()
        self.publish_release.set()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.stack.close()
