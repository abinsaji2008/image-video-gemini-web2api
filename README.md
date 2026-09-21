# image-video-gemini-web2api

A Vercel API wrapper around the reverse-engineered `gemini_webapi` Python client. It uses a Gemini Web browser session cookie rather than a Google Gemini API key.

The upstream library documents cookie authentication, dynamically discovered account models, image generation, and video generation through Gemini Web. citeturn242790view0turn478410view3turn478410view4

## How it works

```text
Postman / your application
        |
        v
https://YOUR_PROJECT.vercel.app/v1/...
        |
        | Vercel rewrite
        v
/api/v1/...
        |
        v
FastAPI
        |
        v
gemini_webapi
        |
        v
Gemini Web session
```

The public `/v1/*` URLs are rewrites to the Vercel Python function. The FastAPI application itself lives at `api/index.py`.

## Authentication

This project does **not** use `GEMINI_API_KEY`.

Set one of these Vercel environment configurations:

### Option A — full cookie

```text
GEMINI_COOKIE=__Secure-1PSID=YOUR_VALUE; __Secure-1PSIDTS=YOUR_VALUE
```

### Option B — separate variables

```text
GEMINI_1PSID=YOUR_VALUE
GEMINI_1PSIDTS=YOUR_VALUE
```

The upstream client documents `__Secure-1PSID` and optional `__Secure-1PSIDTS` for Gemini Web authentication. It also supports automatic cookie refresh while the client is running. citeturn242790view0

Do not commit cookies to GitHub, Postman public collections, issues, logs, or source code.

## Deploy

Import this repository into Vercel.

Then add these Environment Variables:

```text
GEMINI_COOKIE
```

Optional:

```text
GEMINI_TIMEOUT_SEC=240
API_KEYS=my-private-key
```

After changing an environment variable, redeploy the project so the new value is used.

## Health check

```http
GET https://YOUR_PROJECT.vercel.app/
```

Also supported:

```http
GET https://YOUR_PROJECT.vercel.app/health
GET https://YOUR_PROJECT.vercel.app/api
GET https://YOUR_PROJECT.vercel.app/api/health
```

Expected response:

```json
{
  "status": "ok",
  "service": "image-video-gemini-web2api",
  "auth": "gemini-web-cookie",
  "version": "3.0-fastapi-cookie"
}
```

## Models

```http
GET https://YOUR_PROJECT.vercel.app/v1/models
```

The model endpoint initializes the Gemini Web client and returns the models discovered for the authenticated account. The upstream library documents that its model list is dynamic and account-dependent. citeturn478410view1

Use a model ID returned by this endpoint for generation requests. Do not rely on old hard-coded aliases such as `gemini-flash` or `gemini-pro`.

## Postman — Image generation

### Request

```http
POST https://YOUR_PROJECT.vercel.app/v1/images/generations
Content-Type: application/json
Authorization: Bearer my-private-key
```

The Authorization header is required only when `API_KEYS` is configured.

Body:

```json
{
  "model": "gemini-3-flash",
  "prompt": "Generate a cinematic futuristic Kerala city at sunset."
}
```

You can also omit `model`:

```json
{
  "prompt": "Generate a cinematic futuristic Kerala city at sunset."
}
```

The backend explicitly asks Gemini to return a generated image rather than a web image. The upstream package distinguishes generated images from web images and exposes generated images in `response.images`. citeturn478410view3turn870978view0

### Successful response

```json
{
  "created": 1789983914,
  "object": "image.generation",
  "model": "gemini-3-flash",
  "data": [
    {
      "url": "https://...",
      "title": "...",
      "alt": "...",
      "type": "generated"
    }
  ],
  "text": ""
}
```

## Postman — Video generation

### Request

```http
POST https://YOUR_PROJECT.vercel.app/v1/videos/generations
Content-Type: application/json
Authorization: Bearer my-private-key
```

Body:

```json
{
  "model": "gemini-3-pro",
  "prompt": "Generate a short cinematic video of waves on a tropical beach at sunset."
}
```

The upstream package exposes generated videos as `GeneratedVideo` objects in `ModelOutput.videos`. Video generation access can depend on the account and Gemini subscription. citeturn478410view4turn870978view2

## Direct /api URLs

The FastAPI routes are also available directly:

```text
GET  /api
GET  /api/health
GET  /api/v1/models
POST /api/v1/images/generations
POST /api/v1/videos/generations
```

The shorter `/v1/*` URLs are provided by the Vercel rewrites in `vercel.json`.

## Error responses

The API now reports different failure classes instead of returning a generic 404/500:

- `400 invalid_request_error` — malformed JSON, missing prompt, or invalid model type
- `400 invalid_model` — the selected model name is not accepted by Gemini Web
- `401 invalid_api_key` — `API_KEYS` is configured and the request key is wrong
- `503 configuration_error` — the Gemini session cookie is missing
- `502 upstream_error` — Gemini Web or the reverse-engineered client failed
- `502 no_image_generated` — Gemini replied but no generated image was returned
- `502 no_video_generated` — Gemini replied but no generated video was returned
- `504 timeout` — the Gemini request exceeded the configured timeout

## Important limitations

This project uses reverse-engineered Gemini Web internals rather than an official Google API, so compatibility can change when Google changes the Gemini web application or its internal endpoints. citeturn242790view0turn131287view1

Image and video availability can also depend on the account, country/language, school/work account restrictions, age requirements, and subscription. Google's current Gemini Apps help documents separate eligibility rules for image generation/editing, and the upstream library notes that video generation may require an active subscription. citeturn287843search0turn478410view4

## Security

Never place your Gemini cookie in:

- GitHub source files
- README files
- public Postman collections
- GitHub Issues
- client-side JavaScript
- public logs

Store it only in Vercel Environment Variables or another secret store.
