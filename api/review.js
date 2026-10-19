import { db, handler, bad, ID_RE } from "./_db.js";

// A single processed review by its original source review_id.
export default handler(async (q) => {
  const id = String(q.id || "");
  if (!ID_RE.test(id)) throw bad("invalid review id");
  const [r] = await db()`SELECT * FROM reviews WHERE review_id = ${id}`;
  if (!r) throw Object.assign(new Error("review not found"), { status: 404 });
  return { review: r };
});
