import { db, handler, bad, ID_RE } from "./_db.js";

// One issue plus a page of its member reviews (evidence quotes with review IDs).
export default handler(async (q) => {
  const id = String(q.id || "");
  if (!/^ISS-[a-z_]+-[a-z_]+$/.test(id)) throw bad("invalid issue id");
  const offset = Math.max(0, Math.min(1_000_000, parseInt(q.offset || "0", 10) || 0));
  const sev = q.severity && /^[1-5]$/.test(q.severity) ? parseInt(q.severity, 10) : null;
  const sql = db();
  const [issue] = await sql`SELECT * FROM issues WHERE issue_id = ${id}`;
  if (!issue) throw Object.assign(new Error("issue not found"), { status: 404 });
  const members = sev
    ? await sql`SELECT review_id, intent, severity, sentiment, evidence_quote, review_rating, review_likes, review_month, cache_source_id
        FROM reviews WHERE issue_id = ${id} AND severity = ${sev} ORDER BY severity DESC, review_likes DESC, review_id LIMIT 25 OFFSET ${offset}`
    : await sql`SELECT review_id, intent, severity, sentiment, evidence_quote, review_rating, review_likes, review_month, cache_source_id
        FROM reviews WHERE issue_id = ${id} ORDER BY severity DESC, review_likes DESC, review_id LIMIT 25 OFFSET ${offset}`;
  const sevDist = await sql`SELECT severity, count(*)::int AS n FROM reviews WHERE issue_id = ${id} GROUP BY severity ORDER BY severity`;
  return { issue, members, severity_distribution: sevDist, offset };
});
