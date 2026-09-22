// SPDX-License-Identifier: Apache-2.0
// Serves the transpiled source application on a Unix domain socket inside the current
// directory and replays an ordered sanka.http-scenarios/v1 document, writing one
// sanka.http-observations/v1 record per scenario. No TCP listener is opened. With a
// database spec the runner records every captured table and identity sequence after
// each request, and resets the captured tables to the baseline schema first.
"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const MAX_BODY_BYTES = 1048576;

function exchange(socketPath, scenario) {
  return new Promise((resolve, reject) => {
    const payload = "body" in scenario ? JSON.stringify(scenario.body) : null;
    const headers = Object.assign({}, scenario.headers || {});
    if (payload !== null) {
      headers["content-type"] = "application/json";
      headers["content-length"] = String(Buffer.byteLength(payload));
    }
    const request = http.request(
      { socketPath, path: scenario.path, method: scenario.method, headers },
      (response) => {
        const chunks = [];
        let total = 0;
        response.on("data", (chunk) => {
          total += chunk.length;
          if (total > MAX_BODY_BYTES) {
            request.destroy(new Error(`response for ${scenario.id} exceeds ${MAX_BODY_BYTES} bytes`));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let body = null;
          if (text.length) {
            try {
              body = JSON.parse(text);
            } catch {
              reject(new Error(`non-JSON response for ${scenario.id}`));
              return;
            }
          }
          resolve({
            id: scenario.id,
            method: scenario.method,
            path: scenario.path,
            status: response.statusCode,
            media_type: String(response.headers["content-type"] || "").split(";")[0],
            body,
          });
        });
      }
    );
    request.on("error", reject);
    if (payload !== null) request.write(payload);
    request.end();
  });
}

async function observeDatabase(pool, database) {
  const tables = {};
  const sequences = {};
  for (const model of database.models) {
    const columns = model.columns.map((column) => `"${column}"`).join(", ");
    const rows = await pool.query(
      `SELECT ${columns} FROM "${model.table}" ORDER BY "${model.primary_key}"`
    );
    tables[model.table] = rows.rows;
    if (model.auto) {
      const named = await pool.query("SELECT pg_get_serial_sequence($1, $2) AS name", [
        model.table,
        model.primary_key,
      ]);
      const sequence = await pool.query(
        `SELECT last_value::text AS value, is_called FROM ${named.rows[0].name}`
      );
      sequences[model.table] = [sequence.rows[0].value, sequence.rows[0].is_called];
    }
  }
  return { tables, sequences };
}

async function resetDatabase(pool, database) {
  const names = database.models.map((model) => `"${model.table}"`).join(", ");
  await pool.query(`DROP TABLE IF EXISTS ${names} CASCADE`);
  await pool.query(database.schema);
}

function main() {
  const [appPath, casesPath, destination, databasePath] = process.argv.slice(2);
  if (!appPath || !casesPath || !destination) {
    console.error("usage: express-run.js <app.js> <cases.json> <destination> [database.json]");
    process.exit(2);
  }
  const loaded = require(path.resolve(appPath));
  const app = loaded && loaded.__esModule && loaded.default ? loaded.default : loaded;
  if (typeof app !== "function") {
    console.error("the source module does not export an Express application");
    process.exit(2);
  }
  const document = JSON.parse(fs.readFileSync(casesPath, "utf8"));
  if (document.schema !== "sanka.http-scenarios/v1" || !Array.isArray(document.scenarios)) {
    console.error("cases must be a sanka.http-scenarios/v1 document");
    process.exit(2);
  }
  const database = databasePath ? JSON.parse(fs.readFileSync(databasePath, "utf8")) : null;
  const pool = database ? new (require("pg").Pool)({ connectionString: process.env.DATABASE_URL }) : null;
  const socketPath = path.join(process.cwd(), "s.sock");
  const server = http.createServer(app);
  server.listen(socketPath, async () => {
    let code = 0;
    try {
      if (pool && database.reset) await resetDatabase(pool, database);
      const observed = [];
      for (const scenario of document.scenarios) {
        const result = await exchange(socketPath, scenario);
        if (pool) Object.assign(result, await observeDatabase(pool, database));
        observed.push(result);
      }
      fs.writeFileSync(
        destination,
        JSON.stringify({ schema: "sanka.http-observations/v1", observations: observed })
      );
    } catch (error) {
      console.error(String((error && error.message) || error));
      code = 1;
    } finally {
      if (pool) await pool.end();
      // Exit explicitly: a source application holding a database pool would otherwise
      // keep the event loop alive after the server closes.
      server.close(() => process.exit(code));
    }
  });
}

main();
