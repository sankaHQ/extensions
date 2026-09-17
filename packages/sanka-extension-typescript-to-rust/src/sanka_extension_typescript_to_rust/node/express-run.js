// SPDX-License-Identifier: Apache-2.0
// Serves the transpiled source application on a Unix domain socket inside the current
// directory and records the captured GET routes. No TCP listener is opened.
"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const MAX_BODY_BYTES = 1048576;

function request(socketPath, route) {
  return new Promise((resolve, reject) => {
    const client = http.request({ socketPath, path: route, method: "GET" }, (response) => {
      const chunks = [];
      let total = 0;
      response.on("data", (chunk) => {
        total += chunk.length;
        if (total > MAX_BODY_BYTES) {
          client.destroy(new Error(`response for ${route} exceeds ${MAX_BODY_BYTES} bytes`));
          return;
        }
        chunks.push(chunk);
      });
      response.on("end", () => {
        let body;
        try {
          body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        } catch {
          reject(new Error(`non-JSON response for ${route}`));
          return;
        }
        resolve({
          path: route,
          status: response.statusCode,
          media_type: String(response.headers["content-type"] || "").split(";")[0],
          body,
        });
      });
    });
    client.on("error", reject);
    client.end();
  });
}

function main() {
  const [appPath, pathsJson, destination] = process.argv.slice(2);
  if (!appPath || !pathsJson || !destination) {
    console.error("usage: express-run.js <app.js> <paths json> <destination>");
    process.exit(2);
  }
  const loaded = require(path.resolve(appPath));
  const app = loaded && loaded.__esModule && loaded.default ? loaded.default : loaded;
  if (typeof app !== "function") {
    console.error("the source module does not export an Express application");
    process.exit(2);
  }
  const paths = JSON.parse(pathsJson);
  const socketPath = path.join(process.cwd(), "s.sock");
  const server = http.createServer(app);
  server.listen(socketPath, async () => {
    try {
      const observed = [];
      for (const route of paths) observed.push(await request(socketPath, route));
      fs.writeFileSync(destination, JSON.stringify(observed));
    } catch (error) {
      console.error(String((error && error.message) || error));
      process.exitCode = 1;
    } finally {
      // Exit explicitly: a source application holding a database pool would otherwise
      // keep the event loop alive after the server closes.
      server.close(() => process.exit());
    }
  });
}

main();
