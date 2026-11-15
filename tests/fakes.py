"""テスト用の偽の ComfyUI と偽の LLM (http.server)。実際のサーバーには繋がない。"""

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# 1x1 の PNG (画像の出力として返す。次のステップで読み取れるように)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class FakeComfy:
    """/prompt を受けると、polls 回 /history を見たあとで出来上がる。
    fail=True のあいだは、出来上がりをエラーにする。"""

    def __init__(self, polls: int = 1):
        self.polls = polls
        self.fail = False
        self.prompts: dict = {}  # id → {"wf": ..., "seen": n, "video": bool}
        self.received: list = []  # 受けたワークフロー (順に)
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, data, code=200, ctype="application/json"):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/system_stats":
                    return self._send({"system": {}})
                if url.path == "/queue":
                    with owner.lock:
                        running = [[0, pid] for pid, p in owner.prompts.items() if p["seen"] < owner.polls]
                    return self._send({"queue_running": running, "queue_pending": []})
                if url.path.startswith("/history/"):
                    pid = url.path.split("/")[-1]
                    with owner.lock:
                        p = owner.prompts.get(pid)
                        if p is None:
                            return self._send({})
                        p["seen"] += 1
                        if p["seen"] <= owner.polls:
                            return self._send({})
                        if p["fail"]:
                            entry = {"status": {"status_str": "error", "messages": [
                                ["execution_error", {"node_type": "KSampler", "exception_message": "out of memory"}]]},
                                "outputs": {}}
                        else:
                            key = "gifs" if p["video"] else "images"
                            name = f"out_{pid[:6]}." + ("mp4" if p["video"] else "png")
                            entry = {"status": {"status_str": "success"},
                                     "outputs": {"9": {key: [{"filename": name, "subfolder": "", "type": "output"}]}}}
                    return self._send({pid: entry})
                if url.path == "/view":
                    q = parse_qs(url.query)
                    name = q["filename"][0]
                    data = PNG if name.endswith(".png") else ("data:" + name).encode()
                    return self._send(data, ctype="application/octet-stream")
                self._send({}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                url = urlparse(self.path)
                if url.path == "/upload/image":
                    return self._send({"name": "up_" + uuid.uuid4().hex[:6] + ".png", "subfolder": ""})
                if url.path == "/prompt":
                    wf = json.loads(raw)["prompt"]
                    pid = uuid.uuid4().hex
                    video = any("video" in str(n.get("class_type", "")).lower() for n in wf.values())
                    with owner.lock:
                        owner.prompts[pid] = {"wf": wf, "seen": 0, "video": video, "fail": owner.fail}
                        owner.received.append(wf)
                    return self._send({"prompt_id": pid})
                if url.path in ("/queue", "/interrupt"):
                    return self._send({})
                self._send({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeLLM:
    """/chat/completions に、画像があれば「a photo N」、なければ「(元の文) + enhanced」を返す。"""

    def __init__(self):
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                user = body["messages"][-1]["content"]
                if isinstance(user, list):
                    text = f"a photo {len(owner.calls)}"
                else:
                    text = user.strip().splitlines()[-1] + " enhanced"
                owner.calls.append(body)
                data = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def workflow_i2v() -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{prompt}", "clip": ["5", 0]}},
        "3": {"class_type": "PrimitiveFloat", "_meta": {"title": "duration"}, "inputs": {"value": 5.0}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["2", 0], "image": ["1", 0],
                                                   "length": ["3", 0]}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
        "9": {"class_type": "SaveVideo", "inputs": {"video": ["4", 0]}},
    }


def workflow_cn() -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "photo", "clip": ["5", 0]}},
        "3": {"class_type": "ControlNetApplyAdvanced", "inputs": {"positive": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["3", 0]}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }
