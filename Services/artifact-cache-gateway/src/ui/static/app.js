const number = new Intl.NumberFormat("en-US");
const states = ["hit", "bypass", "miss", "refresh", "revalidated", "stale", "error"];
const stateLabels = {
  hit: "S3 fresh hit",
  bypass: "Upstream passthrough",
  miss: "New cache insert",
  refresh: "Cache refresh",
  revalidated: "Revalidation hit",
  stale: "Stale cache fallback",
  error: "Error",
};
const cacheFillLabels = {
  direct: "Domestic source",
  configured_direct: "Origin direct",
  configured_proxy: "Proxy",
};
const cacheModeLabels = {
  all: "Cache both domestic-source and Proxy responses",
  "proxy-only": "Cache only Proxy responses",
};
const ecosystemOrder = ["apt", "pip", "node", "go", "cargo", "dart", "julia", "download", "generic"];
const ecosystemLabels = {
  apt: "APT repository requests",
  pip: "pip / PyPI",
  node: "Node / npm",
  go: "Go Modules",
  cargo: "Cargo / crates.io",
  dart: "Dart Pub",
  julia: "Julia Pkg Server",
  download: "Direct downloads",
  generic: "Other artifacts",
};
const moduleOrder = ["apt", "pypi", "npm", "go", "rust", "dart", "julia", "curl", "git_clone", "unclassified"];
const moduleLabels = {
  apt: "APT",
  pypi: "PyPI",
  npm: "npm",
  go: "Go Modules",
  rust: "Rust / Cargo",
  dart: "Dart Pub",
  julia: "Julia Pkg Server",
  curl: "curl / wget direct downloads",
  git_clone: "Git clone",
  unclassified: "Unclassified history",
};

class GatewayDashboard extends HTMLElement {
  connectedCallback() {
    this.refreshButton = this.querySelector("#refresh");
    this.refreshButton.addEventListener("click", () => this.load(true));
    this.load(true);
    this.timer = window.setInterval(() => this.load(false), 5000);
  }

  disconnectedCallback() { window.clearInterval(this.timer); }

  text(selector, value) { this.querySelector(selector).textContent = value; }

  renderRequests(requests) {
    this.text("#hit-rate", this.percent(requests.hit_rate));
    this.text("#fresh-hit-rate", this.percent(requests.fresh_hit_rate));
    this.text("#request-total", number.format(requests.lookup_total || 0));
    this.text("#error-total", number.format(requests.error || 0));
    const processLookups = requests.current_process?.lookup_total || 0;
    const sessions = requests.historical_sessions || 0;
    const persistence = requests.persistence_error
      ? "Persistence error"
      : `Statistics written every ${number.format(requests.checkpoint_seconds || 0)} seconds max`;
    this.text("#request-scope", requests.scope === "persistent-cumulative"
      ? `${number.format(sessions)} historical sessions + current process ${number.format(processLookups)} lookups; ${persistence}`
      : `Current process ${number.format(processLookups)} lookups`);
    const cacheMode = requests.cache_mode || "unknown";
    this.text("#cache-mode", `Cache mode: ${cacheModeLabels[cacheMode] || "Unknown"}`);
    const grid = this.querySelector("#request-states");
    grid.replaceChildren(...states.map((state) => {
      const item = document.createElement("div");
      item.className = "state";
      const label = document.createElement("span");
      label.textContent = stateLabels[state] || state;
      const value = document.createElement("strong");
      value.textContent = number.format(requests[state] || 0);
      item.append(label, value);
      return item;
    }));
    const upstreamAttempts = requests.upstream_attempts || {};
    const upstreamGrid = this.querySelector("#upstream-attempts");
    upstreamGrid.replaceChildren(...Object.entries(cacheFillLabels).map(([route, labelText]) => {
      const counters = upstreamAttempts[route] || {};
      const item = document.createElement("div");
      item.className = "cache-fill";
      const label = document.createElement("span");
      label.textContent = labelText;
      const results = document.createElement("div");
      results.className = "upstream-result";
      for (const [key, resultText] of [["success", "Hit"], ["failure", "Miss"]]) {
        const result = document.createElement("span");
        result.textContent = resultText;
        const value = document.createElement("strong");
        value.textContent = number.format(counters[key] || 0);
        result.append(value);
        results.append(result);
      }
      item.append(label, results);
      return item;
    }));
    const fills = requests.cache_fills || {};
    const fillGrid = this.querySelector("#cache-fills");
    fillGrid.replaceChildren(...Object.entries(cacheFillLabels).map(([route, labelText]) => {
      const counters = fills[route] || {};
      const item = document.createElement("div");
      item.className = "cache-fill";
      const label = document.createElement("span");
      label.textContent = labelText;
      const objects = document.createElement("strong");
      objects.textContent = `${number.format(counters.objects || 0)} objects`;
      const bytes = document.createElement("small");
      bytes.textContent = this.bytes(counters.bytes || 0);
      item.append(label, objects, bytes);
      return item;
    }));
  }

  renderModules(modules, configuredSources) {
    const sourceLabels = new Map(
      (configuredSources || []).map((source) => [source.name, source.display_name || source.name]),
    );
    const container = this.querySelector("#request-modules");
    const ordered = Object.entries(modules || {}).sort(([left], [right]) => {
      const leftIndex = moduleOrder.indexOf(left);
      const rightIndex = moduleOrder.indexOf(right);
      return (leftIndex < 0 ? moduleOrder.length : leftIndex)
        - (rightIndex < 0 ? moduleOrder.length : rightIndex)
        || left.localeCompare(right);
    });
    container.replaceChildren(...ordered.map(([module, counters]) => {
      const fills = Object.values(counters.cache_fills || {}).reduce(
        (total, row) => ({objects: total.objects + (row.objects || 0), bytes: total.bytes + (row.bytes || 0)}),
        {objects: 0, bytes: 0},
      );
      const attempts = Object.values(counters.upstream_attempts || {}).reduce(
        (total, row) => ({success: total.success + (row.success || 0), failure: total.failure + (row.failure || 0)}),
        {success: 0, failure: 0},
      );
      const card = document.createElement("article");
      card.className = "module-card";
      const title = document.createElement("h3");
      title.textContent = moduleLabels[module] || module;
      const stats = document.createElement("div");
      stats.className = "module-stats";
      for (const [labelText, value] of [
        ["Requests", counters.request_total || 0],
        ["S3 reuse rate", this.percent(counters.hit_rate)],
        ["New / refreshed", (counters.miss || 0) + (counters.refresh || 0)],
        ["Errors", counters.error || 0],
        ["Upstream success", attempts.success],
        ["Upstream failure", attempts.failure],
      ]) {
        const item = document.createElement("span");
        item.textContent = labelText;
        const strong = document.createElement("strong");
        strong.textContent = typeof value === "string" ? value : number.format(value);
        item.append(strong);
        stats.append(item);
      }
      const fill = document.createElement("small");
      fill.textContent = `Wrote ${number.format(fills.objects)} objects / ${this.bytes(fills.bytes)}`;
      const sourceWrap = document.createElement("div");
      sourceWrap.className = "source-stats-wrap";
      const sourceTable = document.createElement("table");
      const sourceHead = document.createElement("thead");
      const sourceHeaderRow = document.createElement("tr");
      for (const labelText of ["Source", "Requests", "S3 reuse rate", "HIT", "New / refreshed", "BYPASS", "Errors", "Upstream success / failure", "Writes"]) {
        const header = document.createElement("th");
        header.textContent = labelText;
        sourceHeaderRow.append(header);
      }
      sourceHead.append(sourceHeaderRow);
      const sourceBody = document.createElement("tbody");
      const sourceEntries = Object.entries(counters.sources || {}).sort(
        ([left], [right]) => left.localeCompare(right),
      );
      const sourceRows = sourceEntries.map(([source, sourceCounters]) => {
        const sourceFills = Object.values(sourceCounters.cache_fills || {}).reduce(
          (total, row) => ({objects: total.objects + (row.objects || 0), bytes: total.bytes + (row.bytes || 0)}),
          {objects: 0, bytes: 0},
        );
        const sourceAttempts = Object.values(sourceCounters.upstream_attempts || {}).reduce(
          (total, row) => ({success: total.success + (row.success || 0), failure: total.failure + (row.failure || 0)}),
          {success: 0, failure: 0},
        );
        const row = document.createElement("tr");
        const name = document.createElement("td");
        name.textContent = module === "git_clone" && source === "github"
          ? "GitHub"
          : sourceLabels.get(source) || source;
        row.append(name);
        for (const value of [
          number.format(sourceCounters.request_total || 0),
          this.percent(sourceCounters.hit_rate),
          number.format(sourceCounters.hit || 0),
          number.format((sourceCounters.miss || 0) + (sourceCounters.refresh || 0)),
          number.format(sourceCounters.bypass || 0),
          number.format(sourceCounters.error || 0),
          `${number.format(sourceAttempts.success)} / ${number.format(sourceAttempts.failure)}`,
          `${number.format(sourceFills.objects)} / ${this.bytes(sourceFills.bytes)}`,
        ]) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
        return row;
      });
      const legacy = counters.historical_unattributed;
      if (legacy) {
        const legacyFills = Object.values(legacy.cache_fills || {}).reduce(
          (total, row) => ({objects: total.objects + (row.objects || 0), bytes: total.bytes + (row.bytes || 0)}),
          {objects: 0, bytes: 0},
        );
        const legacyAttempts = Object.values(legacy.upstream_attempts || {}).reduce(
          (total, row) => ({success: total.success + (row.success || 0), failure: total.failure + (row.failure || 0)}),
          {success: 0, failure: 0},
        );
        const row = document.createElement("tr");
        const name = document.createElement("td");
        name.className = "source-stats-legacy";
        name.textContent = "Not recorded by data source before upgrade (non-source)";
        row.append(name);
        for (const value of [
          number.format(legacy.request_total || 0),
          this.percent(legacy.hit_rate),
          number.format(legacy.hit || 0),
          number.format((legacy.miss || 0) + (legacy.refresh || 0)),
          number.format(legacy.bypass || 0),
          number.format(legacy.error || 0),
          `${number.format(legacyAttempts.success)} / ${number.format(legacyAttempts.failure)}`,
          `${number.format(legacyFills.objects)} / ${this.bytes(legacyFills.bytes)}`,
        ]) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
        sourceRows.push(row);
      }
      sourceBody.append(...sourceRows);
      sourceTable.append(sourceHead, sourceBody);
      sourceWrap.append(sourceTable);
      card.append(title, stats, fill, sourceWrap);
      return card;
    }));
  }

  renderSources(sources) {
    this.text("#source-count", number.format(sources.length));
    const groups = new Map();
    for (const source of sources) {
      const ecosystem = source.ecosystem || "generic";
      if (!groups.has(ecosystem)) groups.set(ecosystem, []);
      groups.get(ecosystem).push(source);
    }
    const ordered = [...groups].sort(([left], [right]) => {
      const leftIndex = ecosystemOrder.indexOf(left);
      const rightIndex = ecosystemOrder.indexOf(right);
      return (leftIndex < 0 ? ecosystemOrder.length : leftIndex)
        - (rightIndex < 0 ? ecosystemOrder.length : rightIndex)
        || left.localeCompare(right);
    });
    const container = this.querySelector("#source-groups");
    container.replaceChildren(...ordered.map(([ecosystem, ecosystemSources]) => {
      const group = document.createElement("div");
      group.className = "source-group";
      group.dataset.ecosystem = ecosystem;
      const heading = document.createElement("div");
      heading.className = "source-group-heading";
      const title = document.createElement("h3");
      title.textContent = ecosystemLabels[ecosystem] || ecosystem;
      const count = document.createElement("span");
      count.textContent = `${number.format(ecosystemSources.length)} sources`;
      heading.append(title, count);

      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      const tableHead = document.createElement("thead");
      const headerRow = document.createElement("tr");
      for (const label of ["Source", "Kind", "Config updated", "Update policy", "Proxy policy", "Metadata TTL", "Upstream chain"]) {
        const header = document.createElement("th");
        header.textContent = label;
        headerRow.append(header);
      }
      tableHead.append(headerRow);
      const body = document.createElement("tbody");
      body.append(...ecosystemSources.map((source) => {
        const row = document.createElement("tr");
        const values = [source.display_name || source.name, source.kind, source.config_updated_at || "Not recorded"];
        for (const value of values) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
        const updatePolicyCell = document.createElement("td");
        const updatePolicy = document.createElement("span");
        updatePolicy.className = `pill ${source.config_update_status}`;
        updatePolicy.textContent = source.config_update_status === "expired"
          ? `Expired ${source.config_expires_at}`
          : source.config_update_policy === "expires"
            ? `Expires ${source.config_expires_at}`
            : source.config_update_status === "unknown"
              ? "Pending admin entry"
              : "Manual (admin)";
        updatePolicyCell.append(updatePolicy);
        row.append(updatePolicyCell);
        const proxyCell = document.createElement("td");
        const proxy = document.createElement("span");
        proxy.className = `pill ${source.proxy_mode}`;
        proxy.textContent = source.proxy_mode;
        proxyCell.append(proxy);
        row.append(proxyCell);
        const ttl = document.createElement("td");
        ttl.textContent = source.metadata_ttl_seconds == null ? "Immutable" : `${source.metadata_ttl_seconds} seconds`;
        row.append(ttl);
        const chain = document.createElement("td");
        chain.textContent = (source.upstream_chain || [])
          .map((item) => `${item.position}:${item.label}/${item.proxy_mode}`)
          .join(" -> ") || "--";
        row.append(chain);
        return row;
      }));
      table.append(tableHead, body);
      wrap.append(table);
      group.append(heading);
      const expiredSources = ecosystemSources.filter(
        (source) => source.config_update_status === "expired",
      );
      if (expiredSources.length) {
        const warning = document.createElement("p");
        warning.className = "source-warning";
        warning.setAttribute("role", "alert");
        warning.textContent = `⚠ ${number.format(expiredSources.length)} sources are due for config review and need manual admin action to review and update their config.`;
        group.append(warning);
      }
      if (ecosystem === "apt") {
        const note = document.createElement("p");
        note.className = "metric-note";
        note.textContent = "OpenSandbox prebuild auto-generates dynamic routes from the originating repository URL, so there is no need to register a source or maintain a mapping; the named configs in this table are only for explicit clients.";
        group.append(note);
      }
      group.append(wrap);
      return group;
    }));
  }

  renderGitMirror(mirror) {
    const container = this.querySelector("#git-mirror-metrics");
    if (!mirror.enabled) {
      this.text("#git-mirror-state", "Not enabled");
      container.replaceChildren();
      return;
    }
    this.text("#git-mirror-state", `${mirror.storage || "Persistent file system"} · ${mirror.upstream_proxy === "configured" ? "Proxy configured" : "Proxy not configured"}`);
    const requests = mirror.requests || {};
    const values = [
      ["Planned repositories", mirror.planned_repositories || 0],
      ["Planned ready", mirror.planned_ready_repositories || 0],
      ["Planned missing", mirror.planned_missing_repositories || 0],
      ["On-demand this process", mirror.on_demand_repositories || 0],
      ["Cached repositories", mirror.ready_repositories || 0],
      ["Real-time / prewarm added", requests.fill || 0],
      ["Mirror hits", requests.hit || 0],
      ["Explicit refresh", requests.refresh || 0],
      ["Errors", requests.error || 0],
    ];
    container.replaceChildren(...values.map(([labelText, rawValue]) => {
      const item = document.createElement("div");
      item.className = "cache-fill";
      const label = document.createElement("span");
      label.textContent = labelText;
      const value = document.createElement("strong");
      value.textContent = number.format(rawValue);
      item.append(label, value);
      return item;
    }));
  }

  percent(value) { return `${((Number(value) || 0) * 100).toFixed(1)}%`; }

  renderFailures(failures) {
    const container = this.querySelector("#recent-failures");
    if (!failures.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No upstream failures recorded in the current process.";
      container.replaceChildren(empty);
      return;
    }
    container.replaceChildren(...failures.map((failure) => {
      const card = document.createElement("article");
      card.className = "failure-card";
      const title = document.createElement("p");
      title.className = "failure-title";
      for (const text of [failure.source, `HTTP ${failure.status}`, failure.stage, failure.url_key]) {
        const value = document.createElement("span");
        value.textContent = text;
        title.append(value);
      }
      const summary = document.createElement("p");
      summary.className = "failure-meta";
      summary.textContent = failure.error;
      const attempts = document.createElement("p");
      attempts.className = "failure-meta";
      attempts.textContent = (failure.attempts || []).map((attempt) => {
        const upstreamStatus = attempt.http_status == null
          ? ""
          : ` upstream HTTP ${attempt.http_status}`;
        return `${attempt.position}:${attempt.label}/${attempt.proxy_mode} ${attempt.stage}${upstreamStatus} ${attempt.error} (${attempt.duration_seconds}s)`;
      }).join(" | ") || "No attempt details";
      card.append(title, summary, attempts);
      return card;
    }));
  }

  inventoryLabel(level, key) {
    if (level === "ecosystems") return ecosystemLabels[key] || key;
    if (key === "@metadata") return "Repository metadata";
    if (key === "@keys") return "Signing keys";
    if (key === "@index") return "Package index";
    if (key === "@unversioned") return "Unversioned";
    return key;
  }

  nextContext(level, key, context) {
    if (level === "ecosystems") return {ecosystem: key};
    if (level === "sources") return {...context, source: key};
    if (level === "packages") return {...context, package: key};
    if (level === "versions") return {...context, version: key};
    return context;
  }

  inventoryURL(context, cursor = null) {
    const query = new URLSearchParams();
    for (const key of ["ecosystem", "source", "package", "version"]) {
      if (context[key]) query.set(key, context[key]);
    }
    query.set("limit", "50");
    if (cursor) query.set("cursor", cursor);
    return `/v1/objects?${query}`;
  }

  createBranch(level, item, context) {
    const branch = document.createElement("details");
    branch.className = "inventory-branch";
    branch.dataset.level = level;
    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.textContent = this.inventoryLabel(level, item.label || item.key);
    const levelLabel = document.createElement("span");
    levelLabel.className = "inventory-level";
    levelLabel.textContent = ({ecosystems: "Ecosystems", sources: "Sources", packages: "Packages", versions: "Versions"})[level] || level;
    summary.append(label, levelLabel);
    const children = document.createElement("div");
    children.className = "inventory-children";
    const next = this.nextContext(level, item.key, context);
    branch.addEventListener("toggle", () => {
      if (branch.open && !branch.dataset.loaded) {
        branch.dataset.loaded = "1";
        this.loadInventoryChildren(children, next);
      }
    });
    branch.append(summary, children);
    return branch;
  }

  artifactCard(item) {
    const card = document.createElement("article");
    card.className = "artifact-card";
    const name = document.createElement("p");
    name.className = "artifact-name";
    name.textContent = item.filename;
    const tags = document.createElement("div");
    tags.className = "artifact-tags";
    for (const value of [item.object_type, item.python_tag, item.abi_tag, item.platform_tag, item.architecture]) {
      if (!value) continue;
      const tag = document.createElement("span");
      tag.className = "artifact-tag";
      tag.textContent = value;
      tags.append(tag);
    }
    const meta = document.createElement("div");
    meta.className = "artifact-meta";
    const values = [
      `Size: ${this.bytes(item.size)}`,
      `Cached at: ${new Date(Number(item.fetched_at) * 1000).toLocaleString("en-US")}`,
      `SHA-256: ${item.digest}`,
      `Source: ${item.source}`,
    ];
    for (const value of values) {
      const row = document.createElement("span");
      row.textContent = value;
      meta.append(row);
    }
    const path = document.createElement("span");
    path.className = "artifact-path";
    path.textContent = `/v1/cache/${item.source}/${item.relative_path}`;
    meta.append(path);
    card.append(name, tags, meta);
    return card;
  }

  bytes(value) {
    let size = Number(value) || 0;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }

  async loadInventoryChildren(container, context, cursor = null, append = false) {
    if (!append) {
      const loading = document.createElement("p");
      loading.className = "inventory-message";
      loading.textContent = "Loading…";
      container.replaceChildren(loading);
    }
    try {
      const response = await fetch(this.inventoryURL(context, cursor), {
        cache: "no-store", headers: {Accept: "application/json"}
      });
      if (!response.ok) throw new Error(`Objects API returned HTTP ${response.status}`);
      const page = await response.json();
      const nodes = page.level === "artifacts"
        ? page.items.map((item) => this.artifactCard(item))
        : page.items.map((item) => this.createBranch(page.level, item, context));
      if (!append) container.replaceChildren();
      container.append(...nodes);
      if (!page.items.length && !append) {
        const empty = document.createElement("p");
        empty.className = "inventory-message";
        empty.textContent = "No cached objects in this group.";
        container.append(empty);
      }
      if (page.next_cursor) {
        const more = document.createElement("button");
        more.className = "load-more";
        more.type = "button";
        more.textContent = "Load more";
        more.addEventListener("click", async () => {
          more.disabled = true;
          more.remove();
          await this.loadInventoryChildren(container, context, page.next_cursor, true);
        });
        container.append(more);
      }
    } catch (reason) {
      const message = document.createElement("p");
      message.className = "inventory-message error";
      message.textContent = `Unable to load cached objects: ${reason.message}`;
      if (!append) container.replaceChildren(message); else container.append(message);
    }
  }

  async renderInventory() {
    const container = this.querySelector("#object-inventory");
    await this.loadInventoryChildren(container, {});
  }

  async load(refreshInventory = false) {
    const health = this.querySelector("#health");
    const error = this.querySelector("#error");
    this.refreshButton.disabled = true;
    try {
      const response = await fetch("/v1/status", {cache: "no-store", headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`Status API returned HTTP ${response.status}`);
      const status = await response.json();
      this.renderRequests(status.requests || {});
      const configuredSources = status.source_details || [];
      this.renderModules(status.requests?.modules || {}, configuredSources);
      this.renderSources(configuredSources);
      this.renderGitMirror(status.git_mirror || {enabled: false});
      this.renderFailures(status.recent_failures || []);
      this.text("#storage", status.storage || "Unknown storage");
      this.text("#updated", `Updated at ${new Date().toLocaleTimeString("en-US")}`);
      if (refreshInventory) await this.renderInventory();
      health.className = "health ok";
      health.lastChild.textContent = "Healthy";
      error.hidden = true;
    } catch (reason) {
      health.className = "health error";
      health.lastChild.textContent = "Unavailable";
      error.textContent = `Unable to load gateway status: ${reason.message}`;
      error.hidden = false;
    } finally {
      this.refreshButton.disabled = false;
    }
  }
}

customElements.define("gateway-dashboard", GatewayDashboard);
