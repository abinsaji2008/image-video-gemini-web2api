import asyncio
import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from gemini_webapi import GeminiClient

DEFAULT_IMAGE_MODEL = "gemini-flash"
DEFAULT_VIDEO_MODEL = "gemini-pro"

_COOKIE = None


def parse_cookie(value: str):
    """Parse a cookie header, JSON cookie export, or key=value list."""
    if not value:
        return None, None

    value = value.strip()

    if value.startswith("{"):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict):
                if isinstance(obj.get("cookie"), str):
                    value = obj["cookie"]
                else:
                    return (
                        obj.get("__Secure-1PSID") or obj.get("1PSID"),
                        obj.get("__Secure-1PSIDTS") or obj.get("1PSIDTS"),
                    )
        except json.JSONDecodeError:
            pass

    pairs = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" in part:
            key, val = part.split("=", 1)
            pairs[key.strip()] = val.strip()

    return (
        pairs.get("__Secure-1PSID") or pairs.get("1PSID"),
        pairs.get("__Secure-1PSIDTS") or pairs.get("1PSIDTS"),
    )


def get_credentials():
    global _COOKIE

    if _COOKIE is not None:
        return _COOKIE

    cookie = os.getenv("GEMINI_COOKIE", "").strip()
    psid, psidts = parse_cookie(cookie)

    psid = psid or os.getenv("GEMINI_1PSID", "").strip()
    psidts = psidts or os.getenv("GEMINI_1PSIDTS", "").strip()

    if not psid:
        raise RuntimeError(
            "Missing Gemini cookie. Set GEMINI_COOKIE with __Secure-1PSID "
            "(and optionally __Secure-1PSIDTS)."
        )

    _COOKIE = (psid, psidts)
    return _COOKIE


def api_authorized(headers):
    keys = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
    if not keys:
        return True

    auth = headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return token in keys


async def make_client():
    psid, psidts = get_credentials()
    client = GeminiClient(psid, psidts)
    await client.init(
        timeout=float(os.getenv("GEMINI_TIMEOUT_SEC", "240")),
        auto_close=False,
        auto_refresh=True,
    )
    return client


async def generate(prompt, model_name=None):
    client = await make_client()
    try:
        selected_model = None
        requested = (model_name or os.getenv("GEMINI_MODEL", "")).strip()

        if requested:
            try:
                selected_model = client.resolve_model(requested)
            except Exception:
                selected_model = None

        return await client.generate_content(
            prompt,
            model=selected_model,
        )
    finally:
        await client.close()


def response_to_dict(response):
    result = {
        "text": response.text or "",
        "images": [],
        "videos": [],
    }

    for image in response.images or []:
        result["images"].append(
            {
                "url": image.url,
                "title": image.title,
                "alt": image.alt,
                "type": (
                    "generated"
                    if image.__class__.__name__ == "GeneratedImage"
                    else "web"
                ),
            }
        )

    for video in response.videos or []:
        result["videos"].append(
            {
                "url": video.url,
                "title": video.title,
                "thumbnail": getattr(video, "thumbnail", None),
            }
        )

    return result


def run(coro):
    return asyncio.run(coro)


def clean_path(raw_path):
    """Normalize Vercel file-function paths to the public API path."""
    path = urlparse(raw_path).path
    prefixes = ("/api/index.py", "/api/index")
    for prefix in prefixes:
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
            break
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return None
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def require_auth(self):
        if api_authorized(self.headers):
            return True
        self.send_json(401, {"error": {"message": "invalid API key"}})
        return False

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        if not self.require_auth():
            return

        path = clean_path(self.path)

        if path == "/":
            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": "image-video-gemini-web2api",
                    "auth": "gemini-web-cookie",
                },
            )
            return

        if path == "/v1/models":
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "gemini-flash",
                            "object": "model",
                            "owned_by": "google-web",
                        },
                        {
                            "id": "gemini-pro",
                            "object": "model",
                            "owned_by": "google-web",
                        },
                    ],
                },
            )
            return

        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.require_auth():
            return

        path = clean_path(self.path)
        body = self.read_json()

        if body is None:
            self.send_json(400, {"error": {"message": "invalid JSON body"}})
            return

        try:
            if path == "/v1/images/generations":
                prompt = body.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    self.send_json(
                        400, {"error": {"message": "prompt is required"}}
                    )
                    return

                response = run(
                    generate(
                        "Generate an image for this request.\n\n"
                        + prompt.strip(),
                        model_name=body.get("model"),
                    )
                )
                data = response_to_dict(response)

                self.send_json(
                    200,
                    {
                        "created": int(time.time()),
                        "object": "image.generation",
                        "model": body.get("model") or DEFAULT_IMAGE_MODEL,
                        "data": data["images"],
                        "text": data["text"],
                    },
                )
                return

            if path == "/v1/videos/generations":
                prompt = body.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    self.send_json(
                        400, {"error": {"message": "prompt is required"}}
                    )
                    return

                response = run(
                    generate(
                        "Generate a video for this request. Create a short video using Gemini's video generation capability.\n\n"
                        + prompt.strip(),
                        model_name=body.get("model"),
                    )
                )
                data = response_to_dict(response)

                self.send_json(
                    200,
                    {
                        "created": int(time.time()),
                        "object": "video.generation",
                        "model": body.get("model") or DEFAULT_VIDEO_MODEL,
                        "data": data["videos"],
                        "text": data["text"],
                    },
                )
                return

            self.send_json(404, {"error": {"message": "not found"}})
        except Exception as exc:
            self.send_json(500, {"error": {"message": str(exc)}})
