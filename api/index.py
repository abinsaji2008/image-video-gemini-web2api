import asyncio
import json
import logging
import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from gemini_webapi import GeminiClient
from vercel.blob import AsyncBlobClient

LOG = logging.getLogger(__name__)
APP_VERSION = "5.0-public-blob-oidc"

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
        pairs[key.strip()] = val.strip()
    return pairs


def parse_cookie(value: str | None) -> tuple[str | None, str | None]:
    """Accept a Cookie header, a JSON cookie export, or KEY=VALUE pairs."""
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
        x.strip()
        for x in os.getenv("API_KEYS", "").split(",")
        if x.strip()
    ]
    if not configured:
        return True

    bearer = request.headers.get("authorization", "")
    token = (
        bearer[7:].strip()
        if bearer.lower().startswith("bearer ")
        else ""
    )
    token = token or request.headers.get("x-api-key", "").strip()
    return token in configured


def error_response(
    status: int, message: str, code: str | None = None
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"message": message}}
    if code:
        payload["error"]["type"] = code
    return JSONResponse(payload, status_code=status)


async def create_gemini_client() -> GeminiClient:
    psid, psidts = get_credentials()
    client = GeminiClient(psid, psidts, proxy=None)
    timeout = float(os.getenv("GEMINI_TIMEOUT_SEC", "240"))

    await client.init(
        timeout=timeout,
        auto_close=False,
        auto_refresh=True,
    )
    return client


async def generate_with_client(
    prompt: str,
    model_name: str | None = None,
):
    client = await create_gemini_client()
    try:
        if model_name:
            response = await client.generate_content(
                prompt,
                model=model_name,
            )
        else:
            response = await client.generate_content(prompt)

        return client, response
    except Exception:
        await client.close()
        raise


async def publish_generated_image(
    image: Any,
    gemini_client: GeminiClient,
    *,
    full_size: bool = False,
) -> dict[str, Any]:
    """Download the Gemini image with its authenticated session and publish it
    as a public Vercel Blob, returning a normal direct URL."""
    tmp_dir = tempfile.mkdtemp(prefix="gemini-image-")
    tmp_path = str(Path(tmp_dir) / "generated.png")

    try:
        saved_path = await image.save(
            path=tmp_dir,
            filename="generated.png",
            verbose=False,
            client=gemini_client.client,
            full_size=full_size,
        )

        file_path = Path(saved_path)
        raw = file_path.read_bytes()

        content_type = "image/png"
        detected = mimetypes.guess_type(file_path.name)[0]
        if detected and detected.startswith("image/"):
            content_type = detected

        pathname = (
            f"gemini/images/"
            f"{time.strftime('%Y/%m/%d')}/"
            f"image-{time.time_ns()}.png"
        )

        # No token is passed explicitly. On Vercel, the SDK resolves
        # the connected project's Blob credentials automatically.
        async with AsyncBlobClient() as blob_client:
            uploaded = await blob_client.put(
                pathname,
                raw,
                access="public",
                content_type=content_type,
                add_random_suffix=True,
            )

        return {
            "url": uploaded.url,
            "title": getattr(image, "title", None),
            "alt": getattr(image, "alt", None),
            "type": "generated",
            "mime_type": content_type,
        }

    finally:
        try:
            if Path(tmp_path).exists():
                Path(tmp_path).unlink()
        except OSError:
            pass

        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass


async def generate_and_publish_images(
    prompt: str,
    model_name: str | None = None,
    full_size: bool = False,
):
    client = await create_gemini_client()
    try:
        if model_name:
            response = await client.generate_content(
                prompt,
                model=model_name,
            )
        else:
            response = await client.generate_content(prompt)

        images: list[dict[str, Any]] = []
        for image in response.images or []:
            if not getattr(image, "url", None):
                continue

            images.append(
                await publish_generated_image(
                    image,
                    client,
                    full_size=full_size,
                )
            )

        return response, images
    finally:
        await client.close()


async def request_json(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None

    return body if isinstance(body, dict) else None


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


@app.get("/api")
@app.get("/api/")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "image-video-gemini-web2api",
        "auth": "gemini-web-cookie",
        "version": APP_VERSION,
        "image_delivery": "public-vercel-blob",
    }


@app.get("/api/v1/models")
async def models(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    try:
        client = await create_gemini_client()
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
            400,
            "invalid JSON body",
            "invalid_request_error",
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(
            400,
            "prompt is required",
            "invalid_request_error",
        )

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return error_response(
            400,
            "model must be a string",
            "invalid_request_error",
        )

    # Default to preview-size download for speed. Set full_size=true when
    # the larger generated image is required.
    full_size = bool(body.get("full_size", False))

    try:
        response, images = await generate_and_publish_images(
            "Generate an image for this request. Return the generated image, "
            "not a web image.\n\n"
            + prompt.strip(),
            model_name=model,
            full_size=full_size,
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
                "The returned url is a public Vercel Blob URL. Put it "
                "directly in an HTML img src; no proxy API or base64 is needed."
            ),
        }

    except ValueError as exc:
        return error_response(400, str(exc), "invalid_model")
    except asyncio.TimeoutError:
        return error_response(
            504,
            "Gemini request timed out",
            "timeout",
        )
    except RuntimeError as exc:
        return error_response(
            503,
            str(exc),
            "configuration_error",
        )
    except Exception as exc:
        LOG.exception("Image generation failed")
        return error_response(
            502,
            f"Gemini image generation failed: {exc}",
            "upstream_error",
        )


@app.post("/api/v1/videos/generations")
async def generate_video(request: Request):
    if not authorize(request):
        return error_response(401, "invalid API key", "invalid_api_key")

    body = await request_json(request)
    if body is None:
        return error_response(
            400,
            "invalid JSON body",
            "invalid_request_error",
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(
            400,
            "prompt is required",
            "invalid_request_error",
        )

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return error_response(
            400,
            "model must be a string",
            "invalid_request_error",
        )

    try:
        client = await create_gemini_client()
        try:
            if model:
                response = await client.generate_content(
                    "Generate a short video for this request using Gemini's "
                    "video generation capability.\n\n"
                    + prompt.strip(),
                    model=model,
                )
            else:
                response = await client.generate_content(
                    "Generate a short video for this request using Gemini's "
                    "video generation capability.\n\n"
                    + prompt.strip()
                )

            videos = [
                media_dict(video, "video")
                for video in (response.videos or [])
                if getattr(video, "url", None)
            ]

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

        finally:
            await client.close()

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
