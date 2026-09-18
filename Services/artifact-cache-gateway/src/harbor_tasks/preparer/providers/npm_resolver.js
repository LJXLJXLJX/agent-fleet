
const fs = require("fs");
const { spawnSync } = require("child_process");

const registry = process.env.NPM_PROBE_REGISTRY.replace(/\/$/, "");
const timeout = String(Math.max(1000, Number(process.env.NPM_PROBE_TIMEOUT) * 1000));

function emit(value) {
  process.stdout.write("ACG\t" + JSON.stringify(value) + "\n");
}

function npmView(arguments_) {
  const result = spawnSync(
    "npm",
    [
      "view",
      ...arguments_,
      "--json",
      "--registry=" + registry,
      "--fetch-retries=1",
      "--fetch-timeout=" + timeout,
      "--loglevel=error",
    ],
    {
      encoding: "utf8",
      timeout: Number(timeout) + 5000,
      env: {
        ...process.env,
        HTTP_PROXY: "",
        HTTPS_PROXY: "",
        ALL_PROXY: "",
        NO_PROXY: "*",
        npm_config_proxy: "",
        npm_config_https_proxy: "",
      },
    },
  );
  if (result.error || result.status !== 0) {
    throw new Error(result.error ? result.error.name : "npm-view-failed");
  }
  return JSON.parse(result.stdout);
}

function resolve(row) {
  const packageName = row.package;
  const requirement = row.requirement;
  if (/^(?:file:|git(?:\+|:)|https?:|workspace:|link:)/i.test(requirement)) {
    emit({package: packageName, requirement, state: "unsupported", reason: "requirement is outside the public npm Registry"});
    return;
  }
  try {
    let versions = npmView([requirement, "version"]);
    if (!Array.isArray(versions)) versions = [versions];
    const version = String(versions[versions.length - 1] || "");
    if (!version) throw new Error("no-version");
    const exact = packageName + "@" + version;
    const metadata = npmView([
      exact,
      "name",
      "version",
      "dist.tarball",
      "dist.integrity",
      "dist.shasum",
    ]);
    const nestedDist = metadata.dist || {};
    const dist = {
      tarball: metadata["dist.tarball"] || nestedDist.tarball,
      integrity: metadata["dist.integrity"] || nestedDist.integrity,
      shasum: metadata["dist.shasum"] || nestedDist.shasum,
    };
    if (metadata.name !== packageName || String(metadata.version || "") !== version || !dist.tarball) {
      throw new Error("invalid-metadata");
    }
    emit({
      package: packageName,
      requirement,
      state: "resolved",
      reason: "npm Registry artifact selected",
      version,
      filename: String(dist.tarball).split("/").pop(),
      url: dist.tarball,
      integrity: dist.integrity || null,
      shasum: dist.shasum || null,
    });
  } catch (error) {
    emit({package: packageName, requirement, state: "unavailable", reason: "npm Registry resolution failed: " + error.message});
  }
}

for (const line of fs.readFileSync("/probe/requirements.jsonl", "utf8").split("\n")) {
  if (line.trim()) resolve(JSON.parse(line));
}
