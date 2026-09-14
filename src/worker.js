const RETENTION_DAYS = 5;
const DEFAULT_KEY = "site/index.html";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = routeToKey(url.pathname);

    if (url.pathname === "/health") {
      return json({ ok: true, service: "daily-impact-news" });
    }

    if (isExpiredKey(key)) {
      return new Response("This article has expired.", { status: 410 });
    }

    const object = await env.NEWS_BUCKET.get(key);
    if (!object && key !== DEFAULT_KEY) {
      const fallback = await env.NEWS_BUCKET.get(DEFAULT_KEY);
      return fallback ? objectResponse(fallback, "text/html; charset=utf-8") : setupResponse();
    }

    if (!object) {
      return setupResponse();
    }

    return objectResponse(object, contentTypeForKey(key));
  }
};

function routeToKey(pathname) {
  const cleanPath = decodeURIComponent(pathname).replace(/^\/+/, "");

  if (!cleanPath) return DEFAULT_KEY;
  if (cleanPath === "data/index.json") return "site/data/index.json";
  if (cleanPath.startsWith("audio/")) return `site/${cleanPath}`;
  if (cleanPath.startsWith("articles/")) return `site/${cleanPath}.html`;
  if (cleanPath.startsWith("topics/")) return `site/${cleanPath}.html`;
  if (cleanPath.endsWith(".html") || cleanPath.endsWith(".json")) return `site/${cleanPath}`;

  return `site/${cleanPath}`;
}

function isExpiredKey(key) {
  const match = key.match(/site\/(?:articles|audio)\/(\d{4}-\d{2}-\d{2})\//);
  if (!match) return false;

  const today = new Date();
  const vancouverDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Vancouver",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(today);

  const ageMs = Date.parse(`${vancouverDate}T00:00:00Z`) - Date.parse(`${match[1]}T00:00:00Z`);
  return ageMs >= RETENTION_DAYS * 24 * 60 * 60 * 1000;
}

function objectResponse(object, fallbackContentType) {
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("content-type", headers.get("content-type") || fallbackContentType);
  headers.set("cache-control", "public, max-age=300");
  return new Response(object.body, { headers });
}

function contentTypeForKey(key) {
  if (key.endsWith(".html")) return "text/html; charset=utf-8";
  if (key.endsWith(".json")) return "application/json; charset=utf-8";
  if (key.endsWith(".mp3")) return "audio/mpeg";
  if (key.endsWith(".css")) return "text/css; charset=utf-8";
  if (key.endsWith(".js")) return "text/javascript; charset=utf-8";
  return "application/octet-stream";
}

function setupResponse() {
  return new Response(
    `<!doctype html>
<html lang="zh-Hans">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>信息差日报</title>
<body style="font-family: system-ui, sans-serif; max-width: 760px; margin: 56px auto; padding: 0 20px; line-height: 1.7;">
  <h1>信息差日报</h1>
  <p>Worker 已启动，R2 中还没有发布内容。下一步是运行 GitHub Actions 的 smoke test 和 daily pipeline。</p>
</body>
</html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } }
  );
}

function json(data) {
  return new Response(JSON.stringify(data), {
    headers: { "content-type": "application/json; charset=utf-8" }
  });
}
