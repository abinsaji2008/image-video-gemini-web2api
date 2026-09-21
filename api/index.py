import asyncio
import json
import logging
import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from gemini_webapi import GeminiClient
from vercel.blob import AsyncBlobClient

LOG = logging.getLogger(__name__)
APP_VERSION = "5.4-clean-temp-media"

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
    oidc_token: str | None = None,
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

        # The Gemini image has been downloaded into Vercel's temporary
        # filesystem. Remove the local copy immediately; only in-memory bytes
        # are kept for the Blob upload.
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass

        content_type = "image/png"
        detected = mimetypes.guess_type(file_path.name)[0]
        if detected and detected.startswith("image/"):
            content_type = detected

        pathname = (
            f"gemini/images/"
            f"{time.strftime('%Y/%m/%d')}/"
            f"image-{time.time_ns()}.png"
        )

        blob_token = (
            os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "").strip()
        )
        oidc_token = (
            (oidc_token or "").strip()
            or os.getenv("VERCEL_OIDC_TOKEN", "").strip()
        )
        store_id = os.getenv("BLOB_STORE_ID", "").strip()

        # New Vercel Blob connections use OIDC. The Python Blob SDK version
        # used by this project still follows the read/write-token path, so
        # handle OIDC directly against the same Vercel Blob API used by the
        # official SDK. BLOB_STORE_ID identifies the connected store.
        if oidc_token and store_id:
            normalized_store_id = (
                store_id[len("store_"):]
                if store_id.startswith("store_")
                else store_id
            )

            query = urlencode({"pathname": pathname})
            blob_api_url = f"https://vercel.com/api/blob/?{query}"
            request_id = (
                f"{normalized_store_id}:{time.time_ns()}"
            )
            headers = {
                "Authorization": f"Bearer {oidc_token}",
                "Content-Type": content_type,
                "x-vercel-blob-store-id": normalized_store_id,
                "x-api-blob-request-id": request_id,
                "x-api-blob-request-attempt": "0",
                "x-api-version": "12",
                "x-content-length": str(len(raw)),
            }

            def upload_oidc() -> dict[str, Any]:
                req = UrlRequest(
                    blob_api_url,
                    data=raw,
                    method="PUT",
                    headers=headers,
                )
                try:
                    with urlopen(req, timeout=120) as response:
                        payload = response.read().decode("utf-8")
                        return json.loads(payload)
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Vercel Blob OIDC upload failed (HTTP {exc.code}): {detail}"
                    ) from exc
                except URLError as exc:
                    raise RuntimeError(
                        f"Vercel Blob OIDC upload connection failed: {exc.reason}"
                    ) from exc

            uploaded = await asyncio.to_thread(upload_oidc)

            return {
                "url": uploaded["url"],
                "title": getattr(image, "title", None),
                "alt": getattr(image, "alt", None),
                "type": "generated",
                "mime_type": content_type,
            }

        # Backwards-compatible path for stores that still provide a token.
        if blob_token:
            async with AsyncBlobClient(token=blob_token) as blob_client:
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

        raise RuntimeError(
            "Vercel Blob OIDC is incomplete. "
            "Missing Vercel OIDC request token or BLOB_STORE_ID. "
            "The deployed Function should receive x-vercel-oidc-token and the "
            "connected Blob store should provide BLOB_STORE_ID. Redeploy after "
            "reconnecting the Blob store."
        )

    finally:
        # Safety cleanup in case download or upload fails before the early
        # cleanup above runs.
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
    oidc_token: str | None = None,
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
                    oidc_token=oidc_token,
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


@app.get("/api", response_class=HTMLResponse)
async def website():
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gemini Image Generator</title>
<style>
body{margin:0;background:#0b1020;color:#eef3ff;font-family:system-ui,sans-serif}
main{max-width:1100px;margin:auto;padding:28px}
h1{margin:0 0 8px}p{color:#9eabc5}
.grid{display:grid;grid-template-columns:360px 1fr;gap:18px}
.card{background:#121a2d;border:1px solid #293650;border-radius:16px;padding:18px}
textarea,input,button{font:inherit}
textarea,input{width:100%;box-sizing:border-box;background:#0b1220;color:#eef3ff;border:1px solid #33415f;border-radius:10px;padding:11px;margin:6px 0 14px}
textarea{min-height:150px;resize:vertical}
.row{display:flex;gap:8px}
button{border:0;border-radius:10px;padding:11px 15px;font-weight:700;cursor:pointer}
#go{background:#5e87ff;color:white;flex:1}#stop{background:#25324d;color:white}
button:disabled{opacity:.5}
.preview{min-height:420px;background:#080e1a;border:1px dashed #33415f;border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden}
#image{max-width:100%;max-height:650px;display:none}
.status{font-size:13px;color:#9eabc5;margin-top:12px}.err{color:#ff9a9a}.ok{color:#8ee0a2}
small{color:#71809a}
pre{background:#080e1a;border:1px solid #293650;border-radius:12px;padding:12px;overflow:auto;max-height:380px;white-space:pre-wrap;word-break:break-word;font-size:12px}
a{color:#9dbbff;word-break:break-all}
@media(max-width:800px){.grid{grid-template-columns:1fr}main{padding:18px}.preview{min-height:320px}}
</style>
</head>
<body>
<main>
<h1>Gemini Image Generator</h1>
<p>Generate through your Gemini Web session. The page keeps the request open while Gemini is working and shows the exact JSON response.</p>
<div class="grid">
<section class="card">
<label>Prompt</label>
<textarea id="prompt" placeholder="A cinematic futuristic Kerala city at sunset"></textarea>
<label>Model (optional)</label>
<input id="model" placeholder="Leave empty for default">
<label>API key (optional)</label>
<input id="key" type="password" placeholder="Only if API_KEYS is configured">
<label><input id="full" type="checkbox" style="width:auto;margin-right:6px"> full-size image</label>
<div class="row">
<button id="go">Generate</button>
<button id="stop" disabled>Cancel</button>
</div>
<div id="status" class="status">Ready.</div>
<div id="time"><small>Elapsed: 0s</small></div>
</section>
<section class="card">
<div class="preview"><div id="empty"><small>No image yet</small></div><img id="image"></div>
<div id="link" style="display:none;margin-top:12px"><small>Direct public image URL</small><br><a id="url" target="_blank" rel="noopener"></a></div>
<div style="display:flex;justify-content:space-between;align-items:center;margin:18px 0 8px"><b>JSON response</b><button id="copy" style="padding:7px 10px;background:#25324d;color:white">Copy JSON</button></div>
<pre id="json">{}</pre>
</section>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
let ctl=null,timer=null,start=0,last={};
function status(t,c=""){$("status").textContent=t;$("status").className="status "+c}
function show(o){last=o;$("json").textContent=JSON.stringify(o,null,2)}
function tick(){start=Date.now();clearInterval(timer);timer=setInterval(()=>{$("time").innerHTML="<small>Elapsed: "+Math.floor((Date.now()-start)/1000)+"s</small>"},1000)}
function stopTick(){clearInterval(timer);timer=null}
$("go").onclick=async()=>{
  const prompt=$("prompt").value.trim();
  if(!prompt){status("Enter a prompt.","err");return}
  ctl=new AbortController();$("go").disabled=true;$("stop").disabled=false;
  $("image").style.display="none";$("empty").style.display="block";$("link").style.display="none";show({});
  status("Generating… this can take more than 30 seconds.");tick();
  const headers={"Content-Type":"application/json"};
  if($("key").value.trim())headers.Authorization="Bearer "+$("key").value.trim();
  const body={prompt};if($("model").value.trim())body.model=$("model").value.trim();if($("full").checked)body.full_size=true;
  try{
    const r=await fetch("/v1/images/generations",{method:"POST",headers,body:JSON.stringify(body),signal:ctl.signal});
    const txt=await r.text();let data;try{data=JSON.parse(txt)}catch{data={raw:txt}}show(data);
    if(!r.ok)throw new Error(data?.error?.message||("HTTP "+r.status));
    const imageUrl=data?.data?.[0]?.url;if(!imageUrl)throw new Error("No image URL returned.");
    $("image").src=imageUrl;$("image").style.display="block";$("empty").style.display="none";
    $("url").href=imageUrl;$("url").textContent=imageUrl;$("link").style.display="block";status("Done.","ok");
  }catch(e){if(e.name==="AbortError")status("Cancelled.");else status(e.message||"Generation failed.","err")}
  finally{$("go").disabled=false;$("stop").disabled=true;ctl=null;stopTick()}
};
$("stop").onclick=()=>{if(ctl)ctl.abort()};
$("copy").onclick=async()=>{try{await navigator.clipboard.writeText(JSON.stringify(last,null,2));$("copy").textContent="Copied";setTimeout(()=>$("copy").textContent="Copy JSON",1000)}catch{}};
</script>
</body>
</html>
        """
    )


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
            oidc_token=request.headers.get("x-vercel-oidc-token"),
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
