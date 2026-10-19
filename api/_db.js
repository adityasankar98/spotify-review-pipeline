// Shared read-only data access. Every query is a fixed, parameterized SELECT; no user input is
// interpolated into SQL. DATABASE_URL is a server-side env var and is never sent to the browser.
import { neon } from "@neondatabase/serverless";

let sql;
export function db() {
  if (!sql) {
    if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL not configured");
    sql = neon(process.env.DATABASE_URL);
  }
  return sql;
}

export function send(res, status, body, maxAge = 300) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", status === 200 ? `public, s-maxage=${maxAge}, stale-while-revalidate=86400` : "no-store");
  res.status(status).send(JSON.stringify(body));
}

export function handler(fn) {
  return async (req, res) => {
    if (req.method !== "GET") return send(res, 405, { error: "method not allowed" });
    try {
      return send(res, 200, await fn(req.query || {}));
    } catch (e) {
      console.error(e);
      return send(res, e.status || 500, { error: e.status ? e.message : "internal error" });
    }
  };
}

export function bad(msg) {
  const e = new Error(msg);
  e.status = 400;
  return e;
}

export const ID_RE = /^[A-Za-z0-9-]{1,64}$/;
