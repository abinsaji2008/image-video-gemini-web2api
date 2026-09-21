import asyncio
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from gemini_webapi import GeminiClient
from vercel.blob import AsyncBlobClient

LOG = logging.getLogger(__name__)
APP_VERSION = "6.2-video-cloudflare-heartbeat"
MEDIA_TTL_SEC = 300

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


def public_download_url(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "download=1"


def blob_oidc_credentials(
    oidc_token: str | None = None,
) -> tuple[str, str | None]:
    token = (
        (oidc_token or "").strip()
        or os.getenv("VERCEL_OIDC_TOKEN", "").strip()
    )
    store_id = os.getenv("BLOB_STORE_ID", "").strip()
    return token, store_id


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
        oidc_token, store_id = blob_oidc_credentials(oidc_token)

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

            url = uploaded["url"]
            return {
                "url": url,
                "download_url": public_download_url(url),
                "title": getattr(image, "title", None),
                "alt": getattr(image, "alt", None),
                "type": "generated",
                "mime_type": content_type,
                "expires_at": int(time.time()) + MEDIA_TTL_SEC,
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

            url = uploaded.url
            return {
                "url": url,
                "download_url": public_download_url(url),
                "title": getattr(image, "title", None),
                "alt": getattr(image, "alt", None),
                "type": "generated",
                "mime_type": content_type,
                "expires_at": int(time.time()) + MEDIA_TTL_SEC,
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


async def publish_generated_video(
    video: Any,
    gemini_client: GeminiClient,
    *,
    oidc_token: str | None = None,
) -> dict[str, Any]:
    """Download a generated Gemini video to temporary storage, upload it to
    public Vercel Blob, then remove the local temporary copy."""
    tmp_dir = tempfile.mkdtemp(prefix="gemini-video-")
    saved_path: Path | None = None

    try:
        saved = await video.save(
            path=tmp_dir,
            verbose=False,
            client=gemini_client.client,
        )

        # gemini-webapi Video.save() returns a dict such as
        # {"video": "/tmp/...mp4", "video_thumbnail": "..."}.
        video_file = saved.get("video") if isinstance(saved, dict) else saved
        if not video_file:
            raise RuntimeError("Gemini returned no downloadable video file.")

        saved_path = Path(video_file)
        if not saved_path.exists():
            raise RuntimeError(
                f"Gemini video download completed without a local file: {saved_path}"
            )

        raw = saved_path.read_bytes()
        content_type = mimetypes.guess_type(saved_path.name)[0] or "video/mp4"
        if not content_type.startswith("video/"):
            content_type = "video/mp4"

        ext = saved_path.suffix.lower() or ".mp4"

        # Delete the downloaded video (and thumbnail, when present) before
        # uploading from memory to Vercel Blob.
        thumbnail_file = (
            Path(saved.get("video_thumbnail"))
            if isinstance(saved, dict) and saved.get("video_thumbnail")
            else None
        )
        try:
            saved_path.unlink(missing_ok=True)
            if thumbnail_file:
                thumbnail_file.unlink(missing_ok=True)
        except OSError:
            pass
        pathname = (
            f"gemini/videos/"
            f"{time.strftime('%Y/%m/%d')}/"
            f"video-{int(time.time() * 1000)}{ext}"
        )

        blob_token = (
            os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
            or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "").strip()
        )
        oidc_token, store_id = blob_oidc_credentials(oidc_token)

        if oidc_token and store_id:
            normalized_store_id = (
                store_id[len("store_"):]
                if store_id.startswith("store_")
                else store_id
            )
            query = urlencode({"pathname": pathname})
            blob_api_url = f"https://vercel.com/api/blob/?{query}"
            request_id = f"{normalized_store_id}:{time.time_ns()}"
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
                    with urlopen(req, timeout=300) as response:
                        payload = response.read().decode("utf-8")
                        return json.loads(payload)
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Vercel Blob OIDC video upload failed (HTTP {exc.code}): {detail}"
                    ) from exc
                except URLError as exc:
                    raise RuntimeError(
                        f"Vercel Blob OIDC video upload connection failed: {exc.reason}"
                    ) from exc

            uploaded = await asyncio.to_thread(upload_oidc)
            url = uploaded["url"]
        elif blob_token:
            async with AsyncBlobClient(token=blob_token) as blob_client:
                uploaded = await blob_client.put(
                    pathname,
                    raw,
                    access="public",
                    content_type=content_type,
                    add_random_suffix=True,
                )
            url = uploaded.url
        else:
            raise RuntimeError(
                "Vercel Blob is not configured. Connect the public Blob store "
                "to this project and redeploy."
            )

        return {
            "url": url,
            "download_url": public_download_url(url),
            "title": getattr(video, "title", None),
            "thumbnail": getattr(video, "thumbnail", None),
            "type": "generated",
            "mime_type": content_type,
            "expires_at": int(time.time()) + MEDIA_TTL_SEC,
        }
    finally:
        if saved_path is not None:
            try:
                saved_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            for item in Path(tmp_dir).iterdir():
                if item.is_file():
                    item.unlink(missing_ok=True)
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
<title>Gemini Image + Video Generator</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#080c16;color:#edf2ff;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1180px;margin:auto;padding:28px}.top{margin-bottom:18px}.top h1{margin:0;font-size:30px}.top p{margin:7px 0 0;color:#8e9ab5}
.tabs{display:flex;gap:8px;margin:16px 0}.tab{padding:10px 18px;border:1px solid #293650;background:#11182a;color:#aeb9d2;border-radius:10px;font-weight:700;cursor:pointer}.tab.active{background:#5d85ff;color:white;border-color:#5d85ff}
.grid{display:grid;grid-template-columns:360px 1fr;gap:18px}.card{background:#10172a;border:1px solid #27344f;border-radius:16px;padding:18px}
label{display:block;margin:0 0 6px;font-size:13px;color:#aab5cb}textarea,input{width:100%;background:#090f1c;color:#edf2ff;border:1px solid #31405d;border-radius:10px;padding:11px;margin-bottom:14px;font:inherit}
textarea{min-height:160px;resize:vertical}.row{display:flex;gap:8px}.btn{border:0;border-radius:10px;padding:11px 15px;font-weight:800;cursor:pointer}.primary{background:#5d85ff;color:#fff;flex:1}.secondary{background:#25324b;color:#fff}.btn:disabled{opacity:.5;cursor:not-allowed}
.preview{min-height:460px;border:1px dashed #33415e;background:#070c16;border-radius:12px;display:flex;align-items:center;justify-content:center;overflow:hidden}.media{display:none;max-width:100%;max-height:680px}.video{width:100%;max-height:680px}.meta{margin-top:12px;padding:12px;background:#0b1120;border:1px solid #26334d;border-radius:10px}.meta a{color:#9eb9ff;word-break:break-all}.download{display:inline-block;margin-top:10px;background:#38a169;color:#fff;text-decoration:none;padding:9px 13px;border-radius:9px;font-weight:800}
.status{font-size:13px;color:#93a0b9;margin-top:11px}.ok{color:#8be0a1}.err{color:#ff9999}.timer{font-size:12px;color:#77859f;margin-top:6px}
pre{margin:0;background:#070c16;border:1px solid #27344f;border-radius:11px;padding:12px;min-height:180px;max-height:330px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12px}
.section-head{display:flex;justify-content:space-between;align-items:center;margin:18px 0 9px}.copy{background:#25324b;color:#fff;border:0;border-radius:8px;padding:7px 10px;cursor:pointer}
.hint{font-size:12px;color:#71809a;margin-top:6px}.hidden{display:none}
@media(max-width:820px){.grid{grid-template-columns:1fr}main{padding:16px}.preview{min-height:320px}}
</style>
</head>
<body>
<main>
<div class="top"><h1>Gemini Image + Video Generator</h1><p>Gemini Web cookies on the server. Generated media is stored in public Vercel Blob and automatically deleted after 5 minutes.</p></div>
<div class="tabs"><button class="tab active" data-kind="image">Image</button><button class="tab" data-kind="video">Video</button></div>
<div class="grid">
<section class="card">
<label>Prompt</label>
<textarea id="prompt" placeholder="A cinematic futuristic Kerala city at sunset"></textarea>
<label>Model (optional)</label>
<input id="model" placeholder="Leave empty for default">
<label>API key (optional)</label>
<input id="key" type="password" placeholder="Only when API_KEYS is configured">
<div class="hint">Video generation may take longer while Gemini renders the video.</div>
<div class="row" style="margin-top:15px"><button id="go" class="btn primary">Generate Image</button><button id="stop" class="btn secondary" disabled>Cancel</button></div>
<div id="status" class="status">Ready.</div><div id="timer" class="timer">Elapsed: 0s</div><div id="ttl" class="timer"></div>
</section>
<section class="card">
<div class="preview"><div id="empty" class="hint">No media yet</div><img id="image" class="media" alt="Generated image"><video id="video" class="media video" controls playsinline></video></div>
<div id="meta" class="meta hidden"><div class="hint">Direct public URL</div><a id="url" target="_blank" rel="noopener"></a><br><a id="download" class="download" target="_blank" rel="noopener">Download</a></div>
<div class="section-head"><b>JSON response</b><button id="copy" class="copy">Copy JSON</button></div>
<pre id="json">{}</pre>
</section>
</div>
</main>
<script>
const $=id=>document.getElementById(id);let kind="image",ctl=null,timerId=null,ttlId=null,last={},expiry=0;
function status(t,c=""){$("status").textContent=t;$("status").className="status "+c}
function show(o){last=o;$("json").textContent=JSON.stringify(o,null,2)}
function elapsed(){const started=Date.now();clearInterval(timerId);timerId=setInterval(()=>$("timer").textContent="Elapsed: "+Math.floor((Date.now()-started)/1000)+"s",1000)}
function stopClock(){clearInterval(timerId);timerId=null}
function clearTTL(){clearInterval(ttlId);ttlId=null;$("ttl").textContent=""}
function setTTL(ts){expiry=ts*1000;clearTTL();const tick=()=>{const left=Math.max(0,expiry-Date.now());const s=Math.floor(left/1000);if(s<=0){$("ttl").textContent="Media expired — cleanup is scheduled.";clearTTL();$("download").removeAttribute("href");return}$("ttl").textContent="Available for download for about "+Math.ceil(s/60)+" min ("+s+"s)"};tick();ttlId=setInterval(tick,1000)}
function resetOutput(){$("image").style.display="none";$("video").style.display="none";$("empty").style.display="block";$("meta").classList.add("hidden");clearTTL()}
function setKind(k){kind=k;$("go").textContent=k==="image"?"Generate Image":"Generate Video";$("prompt").placeholder=k==="image"?"A cinematic futuristic Kerala city at sunset":"A short cinematic video of waves on a tropical beach at sunset";resetOutput()}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");setKind(b.dataset.kind)});
$("go").onclick=async()=>{
 const prompt=$("prompt").value.trim();if(!prompt){status("Enter a prompt.","err");return}
 ctl=new AbortController();$("go").disabled=true;$("stop").disabled=false;resetOutput();show({});status("Generating…");elapsed();
 const headers={"Content-Type":"application/json"};if($("key").value.trim())headers.Authorization="Bearer "+$("key").value.trim();
 const body={prompt};if($("model").value.trim())body.model=$("model").value.trim();
 const endpoint=kind==="image"?"/v1/images/generations":"/v1/videos/generations";
 try{
   const r=await fetch(endpoint,{method:"POST",headers,body:JSON.stringify(body),signal:ctl.signal});
   const txt=await r.text();let data;try{data=JSON.parse(txt)}catch{data={raw:txt}}show(data);
   if(!r.ok)throw new Error(data?.error?.message||("HTTP "+r.status));
   const item=data?.data?.[0];if(!item?.url)throw new Error("No media URL returned.");
   const mediaUrl=item.url;const downloadUrl=item.download_url||mediaUrl+"?download=1";
   if(kind==="image"){$("image").src=mediaUrl;$("image").style.display="block";}else{$("video").src=mediaUrl;$("video").style.display="block";}
   $("empty").style.display="none";$("url").href=mediaUrl;$("url").textContent=mediaUrl;$("download").href=downloadUrl;$("meta").classList.remove("hidden");
   if(item.expires_at)setTTL(item.expires_at);status("Done.","ok");
 }catch(e){if(e.name==="AbortError")status("Cancelled.");else status(e.message||"Generation failed.","err")}
 finally{$("go").disabled=false;$("stop").disabled=true;ctl=null;stopClock()}
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
        "media_ttl_seconds": MEDIA_TTL_SEC,
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
                "directly in an HTML img src; no proxy API or base64 is needed. "
                "The blob is scheduled for automatic deletion after 5 minutes."
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
        return error_response(400, "invalid JSON body", "invalid_request_error")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return error_response(400, "prompt is required", "invalid_request_error")

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return error_response(400, "model must be a string", "invalid_request_error")

    async def stream_result():
        # Cloudflare can return a gateway error when a long video-generation
        # request produces no bytes for a long period. JSON permits whitespace
        # before the value, so send a small heartbeat while Gemini renders.
        # The browser/Postman still receives one normal JSON document at the end.
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(10)
                    await queue.put(" \n")
            except asyncio.CancelledError:
                return

        async def work():
            try:
                client = await create_gemini_client()
                try:
                    generation_prompt = (
                        "Generate a short video for this request using Gemini's "
                        "video generation capability.\n\n"
                        + prompt.strip()
                    )

                    if model:
                        response = await client.generate_content(
                            generation_prompt,
                            model=model,
                        )
                    else:
                        response = await client.generate_content(generation_prompt)

                    videos = []
                    for video in response.videos or []:
                        if not getattr(video, "url", None):
                            continue
                        videos.append(
                            await publish_generated_video(
                                video,
                                client,
                                oidc_token=request.headers.get("x-vercel-oidc-token"),
                            )
                        )

                    if not videos:
                        payload = {
                            "error": {
                                "message": (
                                    "Gemini completed the request but returned no "
                                    "generated video. Your Gemini account or selected "
                                    "model may not have video generation access."
                                ),
                                "type": "no_video_generated",
                            }
                        }
                    else:
                        payload = {
                            "created": int(time.time()),
                            "object": "video.generation",
                            "model": model or "unspecified",
                            "data": videos,
                            "text": response.text or "",
                            "note": (
                                "The returned URL is a public Vercel Blob URL and "
                                "is scheduled for automatic deletion after 5 minutes."
                            ),
                        }
                finally:
                    await client.close()

            except ValueError as exc:
                payload = {
                    "error": {"message": str(exc), "type": "invalid_model"}
                }
            except asyncio.TimeoutError:
                payload = {
                    "error": {"message": "Gemini request timed out", "type": "timeout"}
                }
            except RuntimeError as exc:
                payload = {
                    "error": {"message": str(exc), "type": "configuration_error"}
                }
            except Exception as exc:
                LOG.exception("Video generation failed")
                payload = {
                    "error": {
                        "message": f"Gemini video generation failed: {exc}",
                        "type": "upstream_error",
                    }
                }

            await queue.put(None)
            return payload

        hb = asyncio.create_task(heartbeat())
        task = asyncio.create_task(work())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item.encode("utf-8")

            payload = await task
            yield json.dumps(payload, ensure_ascii=False).encode("utf-8")
        finally:
            hb.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        stream_result(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


async def _list_and_delete_expired_blobs(
    oidc_token: str | None,
) -> dict[str, Any]:
    blob_token = (
        os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
        or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN", "").strip()
    )
    oidc_token, store_id = blob_oidc_credentials(oidc_token)

    deleted = 0
    scanned = 0
    now = time.time()

    if oidc_token and store_id:
        normalized_store_id = (
            store_id[len("store_"):]
            if store_id.startswith("store_")
            else store_id
        )
        cursor: str | None = None

        while True:
            params: dict[str, str] = {"limit": "1000", "prefix": "gemini/"}
            if cursor:
                params["cursor"] = cursor
            url = "https://vercel.com/api/blob/?" + urlencode(params)
            headers = {
                "Authorization": f"Bearer {oidc_token}",
                "x-vercel-blob-store-id": normalized_store_id,
                "x-api-blob-request-id": f"{normalized_store_id}:{time.time_ns()}",
                "x-api-blob-request-attempt": "0",
                "x-api-version": "12",
            }

            def fetch_page(page_url: str = url, page_headers: dict[str, str] = headers):
                req = UrlRequest(page_url, method="GET", headers=page_headers)
                try:
                    with urlopen(req, timeout=30) as response:
                        return json.loads(response.read().decode("utf-8"))
                except HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Vercel Blob list failed (HTTP {exc.code}): {detail}"
                    ) from exc
                except URLError as exc:
                    raise RuntimeError(
                        f"Vercel Blob list connection failed: {exc.reason}"
                    ) from exc

            page = await asyncio.to_thread(fetch_page)
            blobs = page.get("blobs", []) if isinstance(page, dict) else []

            expired_urls: list[str] = []
            for blob in blobs:
                if not isinstance(blob, dict):
                    continue
                scanned += 1
                uploaded_at = blob.get("uploadedAt") or blob.get("uploaded_at")
                if not isinstance(uploaded_at, str):
                    continue
                try:
                    dt = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
                    age = now - dt.timestamp()
                except ValueError:
                    continue
                if age >= MEDIA_TTL_SEC and isinstance(blob.get("url"), str):
                    expired_urls.append(blob["url"])

            for start in range(0, len(expired_urls), 50):
                batch = expired_urls[start:start + 50]
                payload = json.dumps({"urls": batch}).encode("utf-8")
                delete_url = "https://vercel.com/api/blob/delete"
                delete_headers = {
                    "Authorization": f"Bearer {oidc_token}",
                    "Content-Type": "application/json",
                    "x-vercel-blob-store-id": normalized_store_id,
                    "x-api-blob-request-id": f"{normalized_store_id}:{time.time_ns()}",
                    "x-api-blob-request-attempt": "0",
                    "x-api-version": "12",
                }

                def delete_batch(
                    req_url: str = delete_url,
                    req_headers: dict[str, str] = delete_headers,
                    data: bytes = payload,
                ):
                    req = UrlRequest(
                        req_url,
                        data=data,
                        method="POST",
                        headers=req_headers,
                    )
                    try:
                        with urlopen(req, timeout=30) as response:
                            response.read()
                    except HTTPError as exc:
                        detail = exc.read().decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"Vercel Blob delete failed (HTTP {exc.code}): {detail}"
                        ) from exc
                    except URLError as exc:
                        raise RuntimeError(
                            f"Vercel Blob delete connection failed: {exc.reason}"
                        ) from exc

                await asyncio.to_thread(delete_batch)
                deleted += len(batch)

            if not page.get("hasMore") or not page.get("cursor"):
                break
            cursor = str(page["cursor"])

    elif blob_token:
        async with AsyncBlobClient(token=blob_token) as blob_client:
            cursor: str | None = None
            while True:
                listing = await blob_client.list_objects(
                    prefix="gemini/",
                    limit=1000,
                    cursor=cursor,
                )
                expired = []
                for blob in listing.blobs:
                    scanned += 1
                    uploaded_at = getattr(blob, "uploaded_at", None)
                    if uploaded_at is None:
                        continue
                    age = now - uploaded_at.timestamp()
                    if age >= MEDIA_TTL_SEC:
                        expired.append(blob.url)
                if expired:
                    await blob_client.delete(expired)
                    deleted += len(expired)
                if not getattr(listing, "has_more", False):
                    break
                cursor = getattr(listing, "cursor", None)
                if not cursor:
                    break
    else:
        raise RuntimeError(
            "Vercel Blob credentials are unavailable to the cleanup job. "
            "Reconnect the Blob store or configure BLOB_READ_WRITE_TOKEN."
        )

    return {
        "status": "ok",
        "scanned": scanned,
        "deleted": deleted,
        "ttl_seconds": MEDIA_TTL_SEC,
    }


@app.get("/api/cron/cleanup")
async def cleanup_expired_media(request: Request):
    cron_secret = os.getenv("CRON_SECRET", "").strip()
    auth_header = request.headers.get("authorization", "")
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return error_response(401, "invalid cron authorization", "unauthorized")

    try:
        result = await _list_and_delete_expired_blobs(
            request.headers.get("x-vercel-oidc-token")
        )
        return result
    except RuntimeError as exc:
        return error_response(503, str(exc), "configuration_error")
    except Exception as exc:
        LOG.exception("Media cleanup failed")
        return error_response(502, f"Media cleanup failed: {exc}", "cleanup_error")

