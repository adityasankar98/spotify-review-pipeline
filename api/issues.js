import { db, handler } from "./_db.js";

// Baseline issue ranking (priority_score = severity_sum), computed by code and loaded from ranking/aggregates.
export default handler(async () => {
  const rows = await db()`SELECT issue_id, rank, topic, name, description, coherent, complaint_count, severity_sum,
    mean_severity::text AS mean_severity, priority_score, share_of_complaints_pct::text AS share_pct, cancellation_count,
    severity_4plus_count, needs_review_count, example_review_ids FROM issues ORDER BY rank`;
  return { issues: rows };
});
