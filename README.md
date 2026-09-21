# image-video-gemini-web2api

Vercel-ready API wrapper for Google's Gemini image generation and Veo video generation APIs.

## Endpoints

- GET / — health check
- GET /v1/models — image/video model list
- POST /v1/images/generations — image generation
- POST /v1/videos/generations — start video generation
- GET /v1/videos/generations?operation=... — poll a video job

## Deploy to Vercel

1. Import this GitHub repository into Vercel.
2. Add the environment variable:

~~~text
GEMINI_API_KEY=YOUR_GOOGLE_GEMINI_API_KEY
~~~

3. Optional API protection:

~~~text
API_KEYS=your-private-api-key
~~~

When API_KEYS is configured, send Authorization: Bearer your-private-api-key.

## Postman: Image

POST https://YOUR-PROJECT.vercel.app/v1/images/generations

Headers:
~~~text
Content-Type: application/json
Authorization: Bearer your-private-api-key
~~~

Body:
~~~json
{
  "model": "gemini-3.1-flash-image",
  "prompt": "A cinematic futuristic Kerala village at sunset, ultra detailed",
  "aspect_ratio": "16:9",
  "image_size": "1K"
}
~~~

The response contains the generated image as a base64-encoded b64_json field.

## Postman: Video

POST https://YOUR-PROJECT.vercel.app/v1/videos/generations

Body:
~~~json
{
  "model": "veo-3.1-generate-preview",
  "prompt": "A cinematic drone shot of a tropical beach at sunset, waves moving naturally, realistic lighting",
  "aspect_ratio": "16:9",
  "resolution": "1080p"
}
~~~

Video generation is asynchronous. The response returns an operation name. Poll it with:

GET https://YOUR-PROJECT.vercel.app/v1/videos/generations?operation=YOUR_OPERATION

## Image-to-video

The video endpoint accepts a starting image using image_base64 and image_mime_type.

~~~json
{
  "model": "veo-3.1-generate-preview",
  "prompt": "The camera slowly moves forward while the subject smiles naturally",
  "image_base64": "BASE64_IMAGE_DATA",
  "image_mime_type": "image/png",
  "aspect_ratio": "16:9",
  "resolution": "1080p"
}
~~~

## Models

- gemini-3.1-flash-image
- gemini-3-pro-image
- gemini-3.1-flash-lite-image
- veo-3.1-generate-preview

Model availability, access, quotas, and billing depend on the Google Gemini API key used by the deployment.

## Security

Never commit GEMINI_API_KEY or API_KEYS to GitHub. Store secrets in Vercel Environment Variables.

## Official documentation

- https://ai.google.dev/gemini-api/docs/image-generation
- https://ai.google.dev/gemini-api/docs/veo
