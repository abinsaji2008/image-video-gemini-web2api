# image-video-gemini-web2api

A Vercel API wrapper around the reverse-engineered `gemini-webapi` Python client. It uses Gemini Web session cookies instead of a Google Gemini API key.

Generated images are downloaded through the authenticated Gemini session and then uploaded to a **public Vercel Blob**. The API returns the Blob URL, so a website can use it directly:

```html
<img src="https://<store>.public.blob.vercel-storage.com/..." />
```

No base64 response and no image proxy API is needed. Vercel documents that public Blob URLs are directly accessible by anyone who has the URL and can be used directly in HTML. citeturn507324search3turn507324search6

## Required Vercel setup

Create a **PUBLIC** Vercel Blob store and connect it to this project:

1. Open the Vercel project.
2. Open **Storage**.
3. Create **Blob**.
4. Choose **Public** access.
5. Connect the store to this project.
6. Make sure `BLOB_READ_WRITE_TOKEN` is available to the project's production environment.
7. Redeploy.

The project uses Vercel Blob OIDC for production uploads. Vercel documents that OIDC connections use the short-lived `VERCEL_OIDC_TOKEN` together with the connected `BLOB_STORE_ID`, avoiding a long-lived read/write token.

## Environment variables

Gemini Web authentication supports separate sessions for image and video generation.

### Recommended: separate image/video cookies

```text
GEMINI_IMAGE_COOKIE=__Secure-1PSID=IMAGE_PSID; __Secure-1PSIDTS=IMAGE_PSIDTS
GEMINI_VIDEO_COOKIE=__Secure-1PSID=VIDEO_PSID; __Secure-1PSIDTS=VIDEO_PSIDTS
```

Or enter the individual cookie values:

```text
GEMINI_IMAGE_1PSID=IMAGE_PSID
GEMINI_IMAGE_1PSIDTS=IMAGE_PSIDTS

GEMINI_VIDEO_1PSID=VIDEO_PSID
GEMINI_VIDEO_1PSIDTS=VIDEO_PSIDTS
```

The image endpoint uses only the image profile, and the video endpoint uses only the video profile. This allows the two endpoints to use different Gemini Web accounts or sessions.

### Backward compatibility

A single shared cookie is still supported:

```text
GEMINI_COOKIE=__Secure-1PSID=YOUR_VALUE; __Secure-1PSIDTS=YOUR_VALUE
```

or:

```text
GEMINI_1PSID=YOUR_VALUE
GEMINI_1PSIDTS=YOUR_VALUE
```

When a feature-specific cookie is not configured, image/video generation falls back to these legacy variables.


Blob:

```text
BLOB_READ_WRITE_TOKEN=YOUR_VERCEL_BLOB_TOKEN
```

Optional API protection:

```text
API_KEYS=my-private-key
```

Do not put Gemini cookies or Blob tokens in GitHub source code.

## Image API

```http
POST https://YOUR_PROJECT.vercel.app/v1/images/generations
Content-Type: application/json
```

Body:

```json
{
  "prompt": "A cinematic futuristic Kerala city at sunset"
}
```

Optional:

```json
{
  "prompt": "A cinematic futuristic Kerala city at sunset",
  "model": "MODEL_ID_FROM_V1_MODELS",
  "full_size": false
}
```

`full_size=false` is the fast path. Set `full_size=true` when you need the larger image; the upstream library documents that generated-image saving can request a full-size image. citeturn825738view0

### Response

```json
{
  "created": 1789984558,
  "object": "image.generation",
  "model": "unspecified",
  "data": [
    {
      "url": "https://<store>.public.blob.vercel-storage.com/gemini/images/2026/09/21/image-....png",
      "title": "[Generated Image 0]",
      "alt": "watermarked_img.png",
      "type": "generated",
      "mime_type": "image/png"
    }
  ],
  "text": ""
}
```

That `data[0].url` is the final image URL.

### Website usage

Plain HTML:

```html
<img
  src="THE_RETURNED_URL"
  alt="Generated image"
/>
```

JavaScript:

```js
const result = await fetch(
  "https://YOUR_PROJECT.vercel.app/v1/images/generations",
  {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt: "A futuristic Kerala city at sunset"
    })
  }
).then(r => r.json());

const imageUrl = result.data[0].url;
document.querySelector("#result").src = imageUrl;
```

The browser loads the final image directly from Vercel Blob. It does not call `/api/v1/media/image`, and it does not contain base64 in the response.

## Video API

The video endpoint remains:

```http
POST https://YOUR_PROJECT.vercel.app/v1/videos/generations
```

By default, this endpoint uses the Gemini Web **3.1 Pro** model as its coordinating model. Its current Web model identifier is `e6fa609c3fa255c0`.

### Video direct-test mode

For testing Gemini generation time without the extra MP4 download and Vercel Blob upload, send:

```json
{
  "prompt": "A short cinematic video of waves on a tropical beach at sunset",
  "direct_test": true
}
```

In this mode the API returns Gemini's generated-video URL directly. The image endpoint is unaffected, and normal video publishing remains available when `direct_test` is omitted or false.

Body:

```json
{
  "prompt": "A short cinematic video of waves on a tropical beach at sunset"
}
```

The current implementation returns Gemini's video URL directly. Video publishing to public Blob can be added separately because generated videos can be substantially larger.

## Models

```http
GET https://YOUR_PROJECT.vercel.app/v1/models
```

Use an ID returned by this endpoint rather than relying on old hard-coded aliases.

## Health

```http
GET https://YOUR_PROJECT.vercel.app/
```

Expected:

```json
{
  "status": "ok",
  "service": "image-video-gemini-web2api",
  "auth": "gemini-web-cookie",
  "version": "5.1-public-blob-token",
  "image_delivery": "public-vercel-blob"
}
```

## Why the old link did not work

Gemini returned a Google-hosted `lh3.googleusercontent.com/gg-dl/...` URL. That URL is tied to the Gemini image/session delivery path and is not a reliable permanent public asset URL. The upstream `gemini-webapi` image implementation uses an authenticated request client when saving generated images. citeturn825738view0

The new flow is:

```text
Postman / website
        |
        v
Vercel API
        |
        v
Gemini Web cookie
        |
        v
Generated image
        |
        v
Authenticated download
        |
        v
PUBLIC Vercel Blob
        |
        v
https://<store>.public.blob.vercel-storage.com/...
```

## Security

The Blob URL is intentionally public. Anyone who has the URL can retrieve the image. Vercel's documentation distinguishes public Blob delivery from private Blob delivery. citeturn507324search3

Keep these secrets private:

- Gemini Web session cookies
- `BLOB_READ_WRITE_TOKEN`
- `API_KEYS`

## Limitations

This project uses reverse-engineered Gemini Web internals rather than an official Google API, so compatibility can change with the Gemini web application. The underlying library may also be affected by account, region, subscription, or feature availability. citeturn610657view0

## Temporary media and automatic cleanup

Generated images and videos are uploaded to the public Blob store and are marked with a 1 hour TTL. The project runs `/api/cron/cleanup` on the schedule in `vercel.json` and removes expired `gemini/` blobs. Vercel documents that Cron Jobs can be configured in `vercel.json` and triggered in production.

Set a production environment variable:

```text
CRON_SECRET=<long-random-secret>
```

The cron endpoint accepts only `Authorization: Bearer <CRON_SECRET>`. After setting it, redeploy the project so the cron configuration becomes active.

The website provides an **Image / Video** switch, inline preview, direct public URL, **Download** button, JSON response viewer, elapsed time, and an expiry countdown.

Video generation uses `response.videos` and `video.save()` from the upstream `gemini-webapi` package. Video generation availability depends on the Gemini account/model.

The practical deletion point depends on the configured cleanup cron schedule; the application records a 1-hour retention window. Vercel notes that deleted blobs can remain in the CDN cache for up to about one minute.


## Long-running video generation

Video generation now keeps the HTTP connection active with small heartbeat chunks while Gemini renders. The application timeout is 300 seconds, matching the 300-second Vercel Function limit configured in `vercel.json`.

The API no longer forces the old `gemini-omni-1.1-flash` alias. Leave `model` empty to let Gemini Web select the account's available video backend. The implementation also retries a Google "silently aborted" video request only when enough execution time remains.



## Cookie-only image generation

The image endpoint uses Gemini Web cookies only. It does not use `GEMINI_API_KEY`, the Google Gen AI SDK, or another image-generation provider.

The implementation patches Gemini Web's `StreamGenerate` request with the browser image-generation routing parameters and supports both the older generated-image response layout and the newer sparse response layout. The internal image model identifier defaults to `56fdd199312815e2` and can be overridden with `GEMINI_IMAGE_MODEL_ID` if Google rotates it.

Required authentication remains:

```text
GEMINI_COOKIE=__Secure-1PSID=...; __Secure-1PSIDTS=...
```

No separate Gemini API key is required for the image endpoint.
