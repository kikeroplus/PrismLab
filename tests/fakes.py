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
        self.video_bytes = None  # 動画の出力に返す本物の mp4 (None なら中身のない文字)
        self.prompts: dict = {}  # id → {"wf": ..., "seen": n, "video": bool}
        self.received: list = []  # 受けたワークフロー (順に)
        self.uploads: dict = {}  # 受けた画像の名前 → 送られてきた中身 (multipart のまま)
        self.object_info: dict = {}  # /object_info/(ノードの種類) に返すもの (種類 → {"input": ...})
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
                if url.path.startswith("/object_info/"):
                    from urllib.parse import unquote
                    ct = unquote(url.path[len("/object_info/"):])
                    return self._send({ct: owner.object_info[ct]} if ct in owner.object_info else {})
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
                    if name.endswith(".png"):
                        data = PNG
                    elif owner.video_bytes is not None:
                        data = owner.video_bytes
                    else:
                        data = ("data:" + name).encode()
                    return self._send(data, ctype="application/octet-stream")
                self._send({}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                url = urlparse(self.path)
                if url.path == "/upload/image":
                    name = "up_" + uuid.uuid4().hex[:6] + ".png"
                    with owner.lock:
                        owner.uploads[name] = raw  # どの LoadImage にどの画像が入ったか確かめられるように
                    return self._send({"name": name, "subfolder": ""})
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
    """/chat/completions に、画像があれば「a photo N」、英語に直す指示なら「(元の文) in English」、
    ほかは「(元の文) + enhanced」を返す。"""

    def __init__(self):
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                user = body["messages"][-1]["content"]
                system = body["messages"][0]["content"] if len(body["messages"]) > 1 else ""
                if isinstance(user, list):
                    text = f"a photo {len(owner.calls)}"
                elif "英語に翻訳" in system:
                    text = user.strip() + " in English"
                elif "日本語に翻訳" in system:
                    text = "訳: " + user.strip()
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


def workflow_t2i() -> dict:
    return {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{prompt}", "clip": ["5", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["2", 0]}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }


def workflow_i2v_ref() -> dict:
    """I2V + 顔のリファレンス (タイトルが Face Reference の LoadImage)。"""
    wf = workflow_i2v()
    wf["6"] = {"class_type": "LoadImage", "_meta": {"title": "Face Reference"}, "inputs": {"image": "face.png"}}
    wf["4"]["inputs"]["ref"] = ["6", 0]
    return wf


def workflow_flf() -> dict:
    """ファーストフレーム・ラストフレームを指定する I2V (タイトルが First Frame / Last Frame の LoadImage)。"""
    wf = workflow_i2v()
    wf["1"]["_meta"] = {"title": "First Frame"}
    wf["7"] = {"class_type": "LoadImage", "_meta": {"title": "Last Frame"}, "inputs": {"image": "end.png"}}
    wf["4"]["inputs"]["end_image"] = ["7", 0]
    return wf


def workflow_cn() -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "photo", "clip": ["5", 0]}},
        "3": {"class_type": "ControlNetApplyAdvanced", "inputs": {"positive": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["3", 0]}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c"}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }


def make_video(path, colors_rgb, size=(64, 48), fps=8) -> None:
    """色ごとに 1 コマの短い mp4 を作る (物語の「最後のコマ」のテスト用)。"""
    import cv2
    import numpy as np
    writer = cv2.VideoWriter(str(path) + ".tmp.mp4", cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for r, g, b in colors_rgb:
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :] = (b, g, r)
        writer.write(frame)
    writer.release()
    import os
    os.replace(str(path) + ".tmp.mp4", str(path))  # VideoWriter は日本語のパスに書けないことがあるので、名前を変えて置く
