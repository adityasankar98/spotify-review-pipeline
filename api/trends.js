import { db, handler } from "./_db.js";

// Monthly complaint shares by product area (full calendar months, with denominators).
export default handler(async () => {
  const sql = db();
  const rows = await sql`SELECT month, data FROM trends_monthly ORDER BY month`;
  const [cmp] = await sql`SELECT v FROM run_meta WHERE k = 'trends_comparison'`;
  return { months: rows.map((r) => r.data), comparison: cmp ? cmp.v : null };
});
