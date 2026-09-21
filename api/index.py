import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
API_KEYS = [x.strip() for x in os.getenv("API_KEYS", "").split(",") if x.strip()]
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_VIDEO_MODEL = "veo-3.1-generate-preview"


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def authorized(headers):
    if not API_KEYS:
        return True
    value = headers.get("Authorization", "")
    token = value[7:].strip() if value.lower().startswith("bearer ") else ""
    return token in API_KEYS


def google_request(path, method="GET", payload=None, timeout=120):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    url = BASE_URL + path
    data = None if payload is None else json_bytes(payload)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-goog-api-key", GEMINI_API_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "application/json")
            return resp.status, raw, content_type
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, e.headers.get("Content-Type", "application/json")


def error_payload(message, code=400, details=None):
    out = {"error": {"message": message}}
    if details is not None:
        out["error"]["details"] = details
    return code, out


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def aspect_from_size(size):
    if not size or "x" not in size.lower():
        return None
    try:
        w, h = [int(x) for x in size.lower().split("x", 1)]
        ratio = w / h
    except Exception:
        return None
    choices = {
        (1, 1): "1:1",
        (16, 9): "16:9",
        (9, 16): "9:16",
        (4, 3): "4:3",
        (3, 4): "3:4",
        (3, 2): "3:2",
        (2, 3): "2:3",
        (4, 5): "4:5",
        (5, 4): "5:4",
        (21, 9): "21:9",
    }
    best = None
    best_err = 999
    for (w0, h0), label in choices.items():
        err = abs((w0 / h0) - ratio)
        if err < best_err:
            best_err = err
            best = label
    return best if best_err < 0.08 else None


def generate_image(body):
    model = body.get("model") or DEFAULT_IMAGE_MODEL
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_payload("prompt is required")

    input_data = prompt.strip()
    image_b64 = body.get("image_base64")
    image_mime = body.get("image_mime_type", "image/png")
    if image_b64:
        input_data = [
            {"type": "text", "text": prompt.strip()},
            {"type": "image", "data": image_b64, "mime_type": image_mime},
        ]

    response_format = {"type": "image"}
    aspect_ratio = body.get("aspect_ratio") or aspect_from_size(body.get("size"))
    image_size = body.get("image_size")
    if aspect_ratio:
        response_format["aspect_ratio"] = aspect_ratio
    if image_size:
        response_format["image_size"] = str(image_size).upper()

    payload = {
        "model": model,
        "input": input_data,
        "response_format": response_format,
    }
    status, raw, _ = google_request("/interactions", "POST", payload, timeout=120)
    data = parse_json(raw)
    if status >= 400:
        msg = data.get("error", {}).get("message", raw.decode("utf-8", "replace")) if data else raw.decode("utf-8", "replace")
        return error_payload(msg, status, data)

    image_data = None
    mime = "image/png"
    if isinstance(data, dict):
        if data.get("output_image"):
            image_data = data["output_image"].get("data")
            mime = data["output_image"].get("mime_type") or mime
        if not image_data:
            for step in data.get("steps", []) or []:
                for block in step.get("content", []) or []:
                    if block.get("type") == "image" and block.get("data"):
                        image_data = block["data"]
                        mime = block.get("mime_type") or mime
                        break
                if image_data:
                    break

    if not image_data:
        return error_payload("image generation succeeded but no image data was returned", 502, data)

    return 200, {
        "created": int(time.time()),
        "data": [{
            "b64_json": image_data,
            "mime_type": mime,
        }],
        "model": model,
    }


def start_video(body):
    model = body.get("model") or DEFAULT_VIDEO_MODEL
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_payload("prompt is required")
    if not model.startswith("veo-"):
        return error_payload("video endpoint requires a Veo model")

    parameters = {}
    aspect_ratio = body.get("aspect_ratio")
    resolution = body.get("resolution")
    if aspect_ratio:
        parameters["aspectRatio"] = aspect_ratio
    if resolution:
        parameters["resolution"] = str(resolution).lower()

    instance = {"prompt": prompt.strip()}
    image_b64 = body.get("image_base64")
    image_mime = body.get("image_mime_type", "image/png")
    if image_b64:
        instance["image"] = {"bytesBase64Encoded": image_b64, "mimeType": image_mime}

    payload = {"instances": [instance]}
    if parameters:
        payload["parameters"] = parameters

    path = f"/models/{urllib.parse.quote(model, safe='')}:predictLongRunning"
    status, raw, _ = google_request(path, "POST", payload, timeout=120)
    data = parse_json(raw)
    if status >= 400:
        msg = data.get("error", {}).get("message", raw.decode("utf-8", "replace")) if data else raw.decode("utf-8", "replace")
        return error_payload(msg, status, data)

    return 202, {
        "id": data.get("name"),
        "object": "video.generation",
        "status": "processing" if data.get("name") else "unknown",
        "model": model,
        "operation": data.get("name"),
    }


def video_operation(name):
    if not name:
        return error_payload("operation is required")
    name = urllib.parse.unquote(name)
    if name.startswith("/"):
        name = name[1:]
    if not name.startswith("models/"):
        return error_payload("invalid operation name")
    path = "/" + name
    status, raw, _ = google_request(path, "GET", None, timeout=60)
    data = parse_json(raw)
    if status >= 400:
        msg = data.get("error", {}).get("message", raw.decode("utf-8", "replace")) if data else raw.decode("utf-8", "replace")
        return error_payload(msg, status, data)

    out = {
        "id": name,
        "object": "video.generation",
        "status": "completed" if data.get("done") else "processing",
        "operation": name,
    }
    if data.get("error"):
        out["status"] = "failed"
        out["error"] = data["error"]
    response = data.get("response", {}) or {}
    samples = ((response.get("generateVideoResponse") or {}).get("generatedSamples") or [])
    if samples:
        video = samples[0].get("video", {}) or {}
        if video.get("uri"):
            out["video"] = {"url": video["uri"], "mime_type": "video/mp4"}
    return 200, out


class handler(BaseHTTPRequestHandler):
    def _send(self, status, obj, content_type="application/json"):
        raw = json_bytes(obj) if not isinstance(obj, (bytes, bytearray)) else bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(length)

    def _auth(self):
        if not authorized(self.headers):
            self._send(401, {"error": {"message": "invalid API key"}})
            return False
        return True

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if not self._auth():
            return
        path, _, query = self.path.partition("?")
        if path == "/":
            return self._send(200, {
                "status": "ok",
                "service": "image-video-gemini-web2api",
                "image_model": DEFAULT_IMAGE_MODEL,
                "video_model": DEFAULT_VIDEO_MODEL,
            })
        if path == "/v1/models":
            return self._send(200, {
                "object": "list",
                "data": [
                    {"id": DEFAULT_IMAGE_MODEL, "object": "model", "owned_by": "google"},
                    {"id": "gemini-3-pro-image", "object": "model", "owned_by": "google"},
                    {"id": "gemini-3.1-flash-lite-image", "object": "model", "owned_by": "google"},
                    {"id": DEFAULT_VIDEO_MODEL, "object": "model", "owned_by": "google"},
                ],
            })
        if path == "/v1/videos/generations":
            params = urllib.parse.parse_qs(query)
            op = params.get("operation", [""])[0]
            code, obj = video_operation(op)
            return self._send(code, obj)
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        body = parse_json(self._body())
        if body is None:
            return self._send(400, {"error": {"message": "invalid JSON body"}})

        if path == "/v1/images/generations":
            code, obj = generate_image(body)
            return self._send(code, obj)
        if path == "/v1/videos/generations":
            code, obj = start_video(body)
            return self._send(code, obj)
        return self._send(404, {"error": {"message": "not found"}})
