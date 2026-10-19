# Boxing Media Reel API

Backend for the "Boxing Media Reel" iPhone Shortcut. Paste a post link → 3 AI titles → pick one → finished 1080x1920 reel in the Boxing Media template.

- `POST /api/titles` `{url}` → `{titles, video, caption}`
- `POST /api/render` `{url, title, video?, focus?, fit?}` → `{video_url}`
- Header `x-key: <APP_KEY>` on every request

Env vars: `APP_KEY`, `ANTHROPIC_API_KEY`, `APIFY_TOKEN` (Instagram downloads), `BLOB_READ_WRITE_TOKEN` (added automatically when a Blob store is connected).
