import { db, handler } from "./_db.js";

// Overall metrics, topic-group aggregates, AI recommendations and the claims they cite.
export default handler(async () => {
  const sql = db();
  const [status, intents, topics, recs, claims, meta] = await Promise.all([
    sql`SELECT status, count(*)::int AS n FROM reviews GROUP BY status`,
    sql`SELECT intent, count(*)::int AS n FROM reviews WHERE status = 'completed' GROUP BY intent ORDER BY n DESC`,
    sql`SELECT grp, data FROM topic_aggregates`,
    sql`SELECT kind, priority_area, title, body, claim_ids, review_ids, model, check_status FROM recommendations ORDER BY id`,
    sql`SELECT claim_id, subject, metric, value FROM claims ORDER BY claim_id`,
    sql`SELECT k, v FROM run_meta`,
  ]);
  return { status, intents, topics: topics.map((t) => t.data), recommendations: recs, claims,
           meta: Object.fromEntries(meta.map((m) => [m.k, m.v])) };
});
