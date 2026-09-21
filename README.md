# image-video-gemini-web2api

A Vercel wrapper around the reverse-engineered `gemini-webapi` client, using your existing **Gemini Web session cookies** instead of a Google Gemini API key.

The underlying client documents image generation and video generation through the Gemini web app. It uses browser-session authentication and is not an official Google API. citeturn230142search1

## Authentication

Set a Vercel environment variable:

```text
GEMINI_COOKIE=__Secure-1PSID=YOUR_VALUE; __Secure-1PSIDTS=YOUR_VALUE
```

You may also use:

```text
GEMINI_1PSID=YOUR_VALUE
GEMINI_1PSIDTS=YOUR_VALUE
```

Do **not** commit cookies to GitHub. They are account credentials.

The cookie used by this project must contain the Gemini Web session cookie `__Secure-1PSID`; `__Secure-1PSIDTS` is optional but recommended by the underlying client. citeturn386188search0turn230142search1

## Deploy to Vercel

1. Import this GitHub repository into Vercel.
2. Open **Settings → Environment Variables**.
3. Add `GEMINI_COOKIE`.
4. Paste your own Gemini Web cookie value.
5. Deploy/redeploy.

Optional: set `API_KEYS` to protect your Vercel endpoint with Bearer authentication.

```text
API_KEYS=my-private-key
```

Then send:

```http
Authorization: Bearer my-private-key
```

## Test

Health:

```http
GET https://YOUR-PROJECT.vercel.app/
```

Models:

```http
GET https://YOUR-PROJECT.vercel.app/v1/models
```

## Postman — Image

**POST**

```text
https://YOUR-PROJECT.vercel.app/v1/images/generations
```

Headers:

```text
Content-Type: application/json
Authorization: Bearer my-private-key
```

Body:

```json
{
  "model": "gemini-flash",
  "prompt": "Generate a cinematic futuristic Kerala city at sunset."
}
```

The response returns Gemini-generated image objects with URLs and metadata. The underlying library also supports saving generated images with authenticated downloads. citeturn230142search1

## Postman — Video

**POST**

```text
https://YOUR-PROJECT.vercel.app/v1/videos/generations
```

Body:

```json
{
  "model": "gemini-pro",
  "prompt": "Generate a short cinematic video of waves on a tropical beach at sunset."
}
```

The response returns generated video metadata/URL when Gemini returns a generated video. The underlying library documents generated video objects and automatic polling when saving/downloading a rendered video. citeturn230142search1

## What this project uses

```text
Your app / Postman
        ↓
Vercel
        ↓
Gemini Web session cookie
        ↓
Gemini Web internal service
        ↓
Image / Video generation
```

No `GEMINI_API_KEY` is used by this project.

## Important limitations

This relies on reverse-engineered Gemini Web internals rather than an official Google API, so Google can change those internals and break compatibility. Account, region, language, age, and subscription restrictions can also affect whether image/video generation is available. citeturn230142search1

The upstream `gemini-webapi` package currently requires Python 3.11+ and version 2.1.1 is the latest release listed by PyPI. citeturn386188search0

## Security

Never put your Gemini cookies in:
- GitHub source code
- README files
- Postman public collections
- GitHub Issues
- public logs

Store them only as Vercel Environment Variables or another secret store.
