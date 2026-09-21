import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from gemini_webapi import GeminiClient

LOG = logging.getLogger(__name__)
APP_VERSION = "3.0-fastapi-cookie"

app = FastAPI(
    title="Gemini Web Image/Video API",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


def _cookie_pairs(value: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        if key:
            pairs[key] = val.strip()
    return pairs


def parse_cookie(value: str | None) -> tuple[str | None, str | None]:
    """Accept a Cookie header, a JSON export, or KEY=VALUE pairs."""
    if not value:
        return None, None

    raw = value.strip()
    if not raw:
        return None, None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        by_name: dict[str, str] = {}
        for item in parsed:
            if (
                isinstance(item, dict)
                and item.get("name")
                and item.get("value") is not None
            ):
                by_name[str(item["name"])] = str(item["value"])

        return (
            by_name.get("__Secure-1PSID") or by_name.get("1PSID"),
            by_name.get("__Secure-1PSIDTS") or by_name.get("1PSIDTS"),
        )

    if isinstance(parsed, dict):
        nested = parsed.get("cookie")
        if isinstance(nested, str):
            raw = nested
        else:
            return (
                parsed.get("__Secure-1PSID") or parsed.get("1PSID"),
                parsed.get("__Secure-1PSIDTS") or parsed.get("1PSIDTS"),
            )

    pairs = _cookie_pairs(raw)
    return (
        pairs.get("__Secure-1PSID") or pairs.get("1PSID"),
        pairs.get("__Secure-1PSIDTS") or pairs.get("1PSIDTS"),
    )


def get_credentials() -> tuple[str, str | None]:
    psid, psidts = parse_cookie(os.getenv("GEMINI_COOKIE"))
    psid = psid or os.getenv("GEMINI_1PSID", "").strip()
    psidts = psidts or os.getenv("GEMINI_1PSIDTS", "").strip() or None

    if not psid:
        raise RuntimeError(
            "Gemini cookie is not configured. Set GEMINI_COOKIE with "
            "__Secure-1PSID or set GEMINI_1PSID."
        )

    return psid, psidts


def authorize(request: Request) -> bool:
    configured = [
        x.strip() for x in os.getenv("API_KEYS", "").split(",") if x.strip()
    ]
    if not configured:
        return True

    bearer = request.headers.get("authorization", "")
    token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else ""
    token = token or request.headers.get("x-api-key", "").strip()
    return token in configured


def error_response(
    status: int, message: str, code: str | None = None
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"message": message}}
    if code:
        payload["error"]["type"] = code
    return JSONResponse(payload, status_code=status)


async def create_client() -> GeminiClient:
    psid, psidts = get_credentials()
    client = GeminiClient(psid, psidts, proxy=None)
    timeout = float(os.getenv("GEMINI_TIMEOUT_SEC", "240"))
    await client.init(
        timeout=timeout,
        auto_close=False,
        auto_refresh=True,
    )
    return client


async def generate(prompt: str, model_name: str | None = None):
    client = await create_client()
    try:
        # gemini-webapi resolves string model names inside generate_content().
        if model_name:
            return await client.generate_content(prompt, model=model_name)
        return await client.generate_content(prompt)
    finally:
        await client.close()


def make_proxy_url(request: Request, image_url: str) -> str:
    # Keep the response small: return a short API URL that fetches the image
    # later through the authenticated Gemini session.
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/media/image?url={image_url.encode('utf-8').hex()}"


async def fetch_image_with_gemini_session(image_url: str):
    client = await create_client()
    try:
        response = await client.client.get(
            image_url,
            headers={
                "Origin": "https://gemini.google.com",
                "Referer": "https://gemini.google.com/",
            },
            allow_redirects=True,
        )
        return response.status_code, response.headers, response.content
    finally:
        await client.close()


def media_dict(obj: Any, kind: str) -> dict[str, Any]:
    if kind == "image":
        return {
            "url": getattr(obj, "url", None),
            "title": getattr(obj, "title", None),
            "alt": getattr(obj, "alt", None),
            "type": (
                "generated"
                if obj.__class__.__name__ == "GeneratedImage"
                else "web"
            ),
        }

    return {
        "url": getattr(obj, "url", None),
        "title": getattr(obj, "title", None),
        "thumbnail": getattr(obj, "thumbnail", None),
        "type": obj.__class__.__name__,
    }


async def request_json(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@app.get("/api")
@app.get("/api/")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "image-video-gemini-web2api",
        "auth": "gemini-web-cookie",
        "version": APP_VERSION,
    }


@app.get("/api/v1/models")
async def models(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    try:
        client = await create_client()
        try:
            available = client.list_models() or []
            data = []

            for model in available:
                name = getattr(model, "model_name", None)
                if not name:
                    continue

                data.append(
                    {
                        "id": name,
                        "object": "model",
                        "owned_by": "google-web",
                        "name": getattr(model, "display_name", None),
                        "description": getattr(model, "description", None),
                    }
                )

            return {"object": "list", "data": data}
        finally:
            await client.close()

    except RuntimeError as exc:
        return error_response(503, str(exc), "configuration_error")
    except Exception as exc:
        LOG.exception("Model discovery failed")
        return error_response(
            502,
            f"Gemini model discovery failed: {exc}",
            "upstream_error",
        )


@app.post("/api/v1/images/generations")
async def generate_image(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    body = await request_json(request)
    if body is None:
        return error_response(
            400, "invalid JSON body", "invalid_request_error"
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(
            400, "prompt is required", "invalid_request_error"
        )

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return error_response(
            400, "model must be a string", "invalid_request_error"
        )

    try:
        response = await generate(
            "Generate an image for this request. Return the generated image, "
            "not a web image.\n\n"
            + prompt.strip(),
            model_name=model,
        )

        images = []
        for image in response.images or []:
            url = getattr(image, "url", None)
            if not url:
                continue

            images.append(
                {
                    "url": url,
                    "proxy_url": make_proxy_url(request, url),
                    "title": getattr(image, "title", None),
                    "alt": getattr(image, "alt", None),
                    "type": (
                        "generated"
                        if image.__class__.__name__ == "GeneratedImage"
                        else "web"
                    ),
                }
            )

        if not images:
            return error_response(
                502,
                "Gemini completed the request but returned no generated image.",
                "no_image_generated",
            )

        return {
            "created": int(time.time()),
            "object": "image.generation",
            "model": model or "unspecified",
            "data": images,
            "text": response.text or "",
            "note": (
                "Open proxy_url. The proxy fetches the Google-hosted image "
                "using the authenticated Gemini session, so the large "
                "base64 payload is no longer returned by this endpoint."
            ),
        }

    except ValueError as exc:
        return error_response(400, str(exc), "invalid_model")
    except asyncio.TimeoutError:
        return error_response(504, "Gemini request timed out", "timeout")
    except RuntimeError as exc:
        return error_response(503, str(exc), "configuration_error")
    except Exception as exc:
        LOG.exception("Image generation failed")
        return error_response(
            502,
            f"Gemini image generation failed: {exc}",
            "upstream_error",
        )


@app.get("/api/v1/media/image")
async def media_image(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    encoded = request.query_params.get("url", "")
    if not encoded:
        return error_response(400, "url is required", "invalid_request_error")

    try:
        # Hex is used instead of base64 to keep the URL unambiguous in a query string.
        image_url = bytes.fromhex(encoded).decode("utf-8")
    except Exception:
        return error_response(400, "invalid image URL token", "invalid_request_error")

    parsed = urlparse(image_url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "lh3.googleusercontent.com",
        "googleusercontent.com",
    }:
        return error_response(400, "invalid Google image URL", "invalid_request_error")

    try:
        status, headers, content = await fetch_image_with_gemini_session(image_url)
        if status != 200:
            return error_response(
                502,
                f"Google image server returned HTTP {status}",
                "media_upstream_error",
            )

        content_type = headers.get("content-type", "image/png")
        if not content_type.startswith("image/"):
            content_type = "image/png"

        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "Content-Disposition": "inline",
            },
        )

    except asyncio.TimeoutError:
        return error_response(504, "Image download timed out", "timeout")
    except Exception as exc:
        LOG.exception("Image proxy failed")
        return error_response(
            502,
            f"Image proxy failed: {exc}",
            "media_upstream_error",
        )

@app.post("/api/v1/videos/generations")
async def generate_video(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    body = await request_json(request)
    if body is None:
        return error_response(
            400, "invalid JSON body", "invalid_request_error"
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(
            400, "prompt is required", "invalid_request_error"
        )

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return error_response(
            400, "model must be a string", "invalid_request_error"
        )

    try:
        response = await generate(
            "Generate a short video for this request using Gemini's video "
            "generation capability.\n\n"
            + prompt.strip(),
            model_name=model,
        )

        videos = [
            media_dict(video, "video")
            for video in (response.videos or [])
            if getattr(video, "url", None)
        ]

        # Some upstream versions may expose generated video as media.
        if not videos:
            for item in getattr(response, "media", None) or []:
                url = getattr(item, "url", None) or getattr(
                    item, "mp4_url", None
                )
                if url:
                    videos.append(
                        {
                            "url": url,
                            "title": getattr(item, "title", None),
                            "thumbnail": (
                                getattr(item, "thumbnail", None)
                                or getattr(item, "mp4_thumbnail", None)
                            ),
                            "type": item.__class__.__name__,
                        }
                    )

        if not videos:
            return error_response(
                502,
                "Gemini completed the request but returned no generated "
                "video. The account or selected model may not have video "
                "generation access.",
                "no_video_generated",
            )

        return {
            "created": int(time.time()),
            "object": "video.generation",
            "model": model or "unspecified",
            "data": videos,
            "text": response.text or "",
        }

    except ValueError as exc:
        return error_response(400, str(exc), "invalid_model")
    except asyncio.TimeoutError:
        return error_response(504, "Gemini request timed out", "timeout")
    except RuntimeError as exc:
        return error_response(503, str(exc), "configuration_error")
    except Exception as exc:
        LOG.exception("Video generation failed")
        return error_response(
            502,
            f"Gemini video generation failed: {exc}",
            "upstream_error",
        )
