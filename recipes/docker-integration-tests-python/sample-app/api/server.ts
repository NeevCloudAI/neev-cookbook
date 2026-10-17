// A notes API: rows live in Postgres, reads are cached in Redis. Bun ships both clients, so there is nothing to install.
import { RedisClient, SQL } from "bun";

const db = new SQL("postgres://postgres:postgres@127.0.0.1:5432/postgres");
const cache = new RedisClient("redis://127.0.0.1:6379");
await db`CREATE TABLE IF NOT EXISTS notes (id serial PRIMARY KEY, text text NOT NULL)`;

Bun.serve({
  port: 3000,
  routes: {
    "/": new Response(Bun.file(`${import.meta.dir}/index.html`)),
    "/health": () => new Response("ok"),
    "/status": async () => {
      const [{ version }] = await db`SELECT current_setting('server_version') AS version`;
      const info = String(await cache.send("INFO", ["server"]));
      return Response.json({ bun: Bun.version, postgres: version, redis: info.match(/redis_version:(\S+)/)?.[1] });
    },
    "/notes": {
      GET: async () => Response.json(await db`SELECT id, text FROM notes ORDER BY id`),
      POST: async (req) => {
        const { text } = await req.json();
        if (!text) return Response.json({ error: "text is required" }, { status: 400 });
        const [note] = await db`INSERT INTO notes (text) VALUES (${text}) RETURNING id, text`;
        return Response.json(note, { status: 201 });
      },
    },
    "/notes/:id": async (req) => {
      const id = req.params.id;
      if (!/^\d+$/.test(id)) return Response.json({ error: "not found" }, { status: 404 });
      const cached = await cache.get(`note:${id}`);
      if (cached) return new Response(cached, { headers: { "content-type": "application/json", "x-cache": "hit" } });
      const [note] = await db`SELECT id, text FROM notes WHERE id = ${id}`;
      if (!note) return Response.json({ error: "not found" }, { status: 404 });
      await cache.set(`note:${id}`, JSON.stringify(note));
      return Response.json(note, { headers: { "x-cache": "miss" } });
    },
  },
});
console.log("api listening on :3000");
