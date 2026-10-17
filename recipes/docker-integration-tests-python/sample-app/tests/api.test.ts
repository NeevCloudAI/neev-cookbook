// Integration tests: real HTTP calls to the API, which talks to the real Postgres and Redis.
import { expect, test } from "bun:test";

const API = "http://127.0.0.1:3000";
const post = (body: unknown) =>
  fetch(`${API}/notes`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

test("health check answers", async () => {
  expect(await (await fetch(`${API}/health`)).text()).toBe("ok");
});

test("creating a note stores it in Postgres", async () => {
  const res = await post({ text: "buy milk" });
  expect(res.status).toBe(201);
  expect(await res.json()).toMatchObject({ id: expect.any(Number), text: "buy milk" });
});

test("a note without text is rejected", async () => {
  expect((await post({})).status).toBe(400);
});

test("the second read is served from the Redis cache", async () => {
  const { id } = await (await post({ text: "cache me" })).json();
  const first = await fetch(`${API}/notes/${id}`);
  const second = await fetch(`${API}/notes/${id}`);
  expect(first.headers.get("x-cache")).toBe("miss");
  expect(second.headers.get("x-cache")).toBe("hit");
  expect(await second.json()).toEqual({ id, text: "cache me" });
});

test("an unknown note is a 404", async () => {
  expect((await fetch(`${API}/notes/999999`)).status).toBe(404);
});
