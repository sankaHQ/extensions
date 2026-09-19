import express from "express";
import { Pool } from "pg";

const app = express();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });
app.use(express.json());

app.get("/widgets", async (_req, res) => {
  const { rows } = await pool.query("SELECT id, name, count, enabled, note FROM widgets ORDER BY id LIMIT $1", [100]);
  res.json(rows);
});

app.get("/widgets/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) {
    return res.status(404).json({ error: "not found" });
  }
  const { rows } = await pool.query("SELECT id, name, count, enabled, note FROM widgets WHERE id = $1", [id]);
  if (rows.length === 0) {
    return res.status(404).json({ error: "not found" });
  }
  return res.json(rows[0]);
});

app.post("/widgets", async (req, res) => {
  const body = req.body;
  if (
    typeof body !== "object" ||
    body === null ||
    Array.isArray(body) ||
    Object.keys(body).some((key) => !["name", "count", "enabled", "note"].includes(key)) ||
    typeof body.name !== "string" ||
    typeof body.count !== "number" ||
    !Number.isInteger(body.count) ||
    body.count < -2147483648 ||
    body.count > 2147483647 ||
    typeof body.enabled !== "boolean" ||
    (body.note !== undefined && body.note !== null && typeof body.note !== "string")
  ) {
    return res.status(400).json({ error: "invalid request body" });
  }
  try {
    const { rows } = await pool.query(
      "INSERT INTO widgets (name, count, enabled, note) VALUES ($1, $2, $3, $4) RETURNING id, name, count, enabled, note",
      [body.name, body.count, body.enabled, body.note ?? null]
    );
    return res.status(201).json(rows[0]);
  } catch (error) {
    if ((error as { code?: string }).code === "23505") {
      return res.status(409).json({ error: "conflict" });
    }
    throw error;
  }
});

app.patch("/widgets/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) {
    return res.status(404).json({ error: "not found" });
  }
  const body = req.body;
  if (
    typeof body !== "object" ||
    body === null ||
    Array.isArray(body) ||
    Object.keys(body).some((key) => !["name", "count", "enabled", "note"].includes(key)) ||
    (body.name !== undefined && (typeof body.name !== "string")) ||
    (body.count !== undefined && (typeof body.count !== "number" || !Number.isInteger(body.count) || body.count < -2147483648 || body.count > 2147483647)) ||
    (body.enabled !== undefined && (typeof body.enabled !== "boolean")) ||
    (body.note !== undefined && body.note !== null && (typeof body.note !== "string"))
  ) {
    return res.status(400).json({ error: "invalid request body" });
  }
  try {
    const { rows } = await pool.query(
      "UPDATE widgets SET name = CASE WHEN $2 THEN $3 ELSE name END, count = CASE WHEN $4 THEN $5 ELSE count END, enabled = CASE WHEN $6 THEN $7 ELSE enabled END, note = CASE WHEN $8 THEN $9 ELSE note END WHERE id = $1 RETURNING id, name, count, enabled, note",
      [id, body.name !== undefined, body.name ?? null, body.count !== undefined, body.count ?? null, body.enabled !== undefined, body.enabled ?? null, body.note !== undefined, body.note ?? null]
    );
    if (rows.length === 0) {
      return res.status(404).json({ error: "not found" });
    }
    return res.json(rows[0]);
  } catch (error) {
    if ((error as { code?: string }).code === "23505") {
      return res.status(409).json({ error: "conflict" });
    }
    throw error;
  }
});

app.put("/widgets/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) {
    return res.status(404).json({ error: "not found" });
  }
  const body = req.body;
  if (
    typeof body !== "object" ||
    body === null ||
    Array.isArray(body) ||
    Object.keys(body).some((key) => !["name", "count", "enabled", "note"].includes(key)) ||
    typeof body.name !== "string" ||
    typeof body.count !== "number" ||
    !Number.isInteger(body.count) ||
    body.count < -2147483648 ||
    body.count > 2147483647 ||
    typeof body.enabled !== "boolean" ||
    (body.note !== undefined && body.note !== null && typeof body.note !== "string")
  ) {
    return res.status(400).json({ error: "invalid request body" });
  }
  try {
    const { rows } = await pool.query(
      "UPDATE widgets SET name = $2, count = $3, enabled = $4, note = $5 WHERE id = $1 RETURNING id, name, count, enabled, note",
      [id, body.name, body.count, body.enabled, body.note ?? null]
    );
    if (rows.length === 0) {
      return res.status(404).json({ error: "not found" });
    }
    return res.json(rows[0]);
  } catch (error) {
    if ((error as { code?: string }).code === "23505") {
      return res.status(409).json({ error: "conflict" });
    }
    throw error;
  }
});

app.delete("/widgets/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) {
    return res.status(404).json({ error: "not found" });
  }
  const { rows } = await pool.query("DELETE FROM widgets WHERE id = $1 RETURNING id", [id]);
  if (rows.length === 0) {
    return res.status(404).json({ error: "not found" });
  }
  return res.status(204).end();
});

export default app;
