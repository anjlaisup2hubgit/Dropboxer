// Handshake relay for the web version. It only passes two short, encrypted text blobs (WebRTC offer and answer)
// between the PC page and the phone page, and forgets them after 5 minutes. Photos never come near it.
// Needs a Redis database (Upstash, via the Vercel Marketplace) providing UPSTASH_REDIS_REST_URL / _TOKEN
// (the older KV_REST_API_URL / KV_REST_API_TOKEN names also work).
const URL_ = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;

async function redis(cmd) {
  const r = await fetch(URL_, { method: "POST", headers: { Authorization: `Bearer ${TOKEN}` }, body: JSON.stringify(cmd) });
  if (!r.ok) throw new Error(`redis ${r.status}`);
  return (await r.json()).result;
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (!URL_ || !TOKEN) return res.status(500).send("relay storage is not configured");
  const q = req.method === "GET" ? req.query : req.body || {};
  const { room, kind } = q;
  if (!/^[A-Za-z0-9_-]{20,64}$/.test(room || "") || !["offer", "answer"].includes(kind)) return res.status(400).send("bad request");
  const key = `pt:${room}:${kind}`;
  try {
    if (req.method === "POST") {
      const data = String(q.data || "");
      if (!data || data.length > 20000) return res.status(400).send("bad data");
      if (kind === "offer") await redis(["SET", key, data, "EX", "300"]);
      else if (!(await redis(["SET", key, data, "EX", "300", "NX"]))) return res.status(409).send("already answered");
      return res.status(204).end();
    }
    if (req.method === "GET") {
      const v = await redis(["GET", key]);
      return v ? res.status(200).send(v) : res.status(404).send("");
    }
    return res.status(405).send("method not allowed");
  } catch (e) {
    return res.status(502).send("relay error");
  }
}
