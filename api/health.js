import { db, handler } from "./_db.js";

export default handler(async () => {
  const [r] = await db()`SELECT count(*)::int AS reviews FROM reviews`;
  return { ok: true, reviews_in_database: r.reviews };
});
