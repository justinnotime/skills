"use strict";
const $ = (id) => document.getElementById(id);
let projects = [],
  records = [],
  previewRequest = null,
  query = "",
  kind = "",
  order = "recent";
const textExtensions = new Set(
  "md markdown txt log csv json yaml yml toml py js cjs mjs ts tsx jsx sh bash html css xml sql rs go c cpp h diff patch".split(
    " ",
  ),
);
const imageExtensions = new Set(["png", "jpg", "jpeg", "gif", "webp", "avif"]);
const node = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
function state() {
  const p = new URLSearchParams(location.hash.slice(1));
  return {
    project: p.get("project") || "",
    folder: p.get("folder") || "",
    revision: p.get("revision") || "",
  };
}
function route(project = "", folder = "", revision = "") {
  const p = new URLSearchParams();
  if (project) p.set("project", project);
  if (folder) p.set("folder", folder);
  if (revision) p.set("revision", revision);
  return "#" + p;
}
function extension(name) {
  return name.includes(".") ? name.split(".").pop().toLowerCase() : "";
}
function category(name) {
  const e = extension(name);
  return imageExtensions.has(e)
    ? "image"
    : ["md", "markdown", "txt", "pdf", "html"].includes(e)
      ? "document"
      : ["json", "csv", "yaml", "yml", "toml", "xml"].includes(e)
        ? "data"
        : textExtensions.has(e)
          ? "code"
          : "other";
}
function size(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}
function date(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function fileUrl(file) {
  return (
    "projects/" +
    [file.project, file.revision, "files", ...file.name.split("/")]
      .map(encodeURIComponent)
      .join("/")
  );
}
function latestFiles(project) {
  const names = new Set();
  return records
    .filter((f) => f.project === project)
    .filter((f) => {
      if (names.has(f.name)) return false;
      names.add(f.name);
      return true;
    });
}
function makeLink(text, href, cls) {
  const a = node("a", text, cls);
  a.href = href;
  return a;
}
function render() {
  const s = state(),
    selected = projects.find((p) => p.project === s.project),
    latest = selected?.releases[0];
  $("title").textContent = selected ? s.project : "全部任务";
  $("summary").textContent = latest?.title || "按任务查找报告、图片和数据文件";
  $("projects").replaceChildren();
  const choices = [
    { id: "", label: "全部任务", count: projects.length },
    ...projects.map((p) => ({
      id: p.project,
      label: p.project,
      count: latestFiles(p.project).length,
    })),
  ];
  for (const p of choices) {
    const a = makeLink("", route(p.id));
    a.append(node("span", p.label), node("span", p.count, "count"));
    if (p.id === s.project) a.setAttribute("aria-current", "page");
    $("projects").append(a);
  }
  $("open-entry").hidden = !latest;
  if (latest) {
    const entry = {
      ...latest,
      ...latest.files[latest.entry],
      name: latest.entry,
    };
    $("open-entry").href = fileUrl(entry);
    $("open-entry").onclick = (event) => {
      if (extension(entry.name) !== "html") {
        event.preventDefault();
        preview(entry);
      }
    };
  }
  $("version-label").hidden = !selected;
  $("version").replaceChildren(new Option("全部文件 · 每个路径的最新内容", ""));
  if (selected)
    for (const r of selected.releases)
      $("version").add(
        new Option(date(r.published_at) + " · " + r.title, r.revision),
      );
  $("version").value = s.revision;
  const crumbs = $("breadcrumbs");
  crumbs.replaceChildren(makeLink("全部任务", route()));
  if (s.project) {
    crumbs.append(
      node("span", "/"),
      makeLink(s.project, route(s.project, "", s.revision)),
    );
    let path = "";
    for (const part of s.folder.split("/").filter(Boolean)) {
      path += part + "/";
      crumbs.append(
        node("span", "/"),
        makeLink(part, route(s.project, path, s.revision)),
      );
    }
  }
  let files = selected
    ? s.revision
      ? records.filter(
          (f) => f.project === s.project && f.revision === s.revision,
        )
      : latestFiles(s.project)
    : projects.flatMap((p) => latestFiles(p.project));
  if (s.project && !selected) files = [];
  files = files.filter(
    (f) =>
      (!s.folder || f.name.startsWith(s.folder)) &&
      (!kind || category(f.name) === kind) &&
      (!query ||
        [f.name, f.project, f.title].some((v) =>
          v.toLocaleLowerCase().includes(query),
        )),
  );
  files.sort((a, b) =>
    order === "name"
      ? a.name.localeCompare(b.name)
      : order === "size"
        ? b.bytes - a.bytes
        : b.published_at.localeCompare(a.published_at) ||
          a.name.localeCompare(b.name),
  );
  const body = $("files");
  body.replaceChildren();
  let count = 0;
  const folders = new Map();
  if (selected && !query && !kind)
    for (const f of files) {
      const rest = f.name.slice(s.folder.length);
      if (rest.includes("/")) {
        const folder = rest.split("/")[0];
        folders.set(folder, (folders.get(folder) || 0) + 1);
      }
    }
  for (const [folder, n] of [...folders].sort((a, b) =>
    a[0].localeCompare(b[0]),
  )) {
    const tr = node("tr"),
      td = node("td"),
      wrap = node("div", undefined, "file-name"),
      a = makeLink(
        folder,
        route(s.project, s.folder + folder + "/", s.revision),
      );
    wrap.append(node("span", "DIR", "file-icon folder-icon"), a);
    td.append(wrap);
    tr.append(
      td,
      node("td", s.project, "project-cell"),
      node("td", n + " 个文件", "date"),
      node("td", "—", "size"),
      node("td"),
    );
    body.append(tr);
    count++;
  }
  for (const f of files) {
    if (
      selected &&
      !query &&
      !kind &&
      f.name.slice(s.folder.length).includes("/")
    )
      continue;
    const tr = node("tr"),
      td = node("td"),
      wrap = node("div", undefined, "file-name"),
      labels = node("div"),
      button = node("button", f.name.split("/").pop());
    button.type = "button";
    button.onclick = () => preview(f);
    labels.append(button);
    if (query || !selected || kind)
      labels.append(node("div", f.name, "file-path"));
    wrap.append(node("span", extension(f.name) || "file", "file-icon"), labels);
    td.append(wrap);
    const projectCell = node("td", undefined, "project-cell");
    projectCell.append(makeLink(f.project, route(f.project)));
    const download = makeLink("下载", fileUrl(f), "download");
    download.download = f.name.split("/").pop();
    const action = node("td");
    action.append(download);
    tr.append(
      td,
      projectCell,
      node("td", date(f.published_at), "date"),
      node("td", size(f.bytes), "size"),
      action,
    );
    body.append(tr);
    count++;
  }
  $("empty").hidden = count > 0;
  $("status").textContent =
    files.length +
    " 个文件 · " +
    (selected
      ? selected.releases.length + " 个发布版本"
      : projects.length + " 个任务");
}
// Markdown is rendered from text nodes only: embedded HTML and script never execute.
function markdown(text) {
  const out = node("div", undefined, "markdown"),
    lines = text.replace(/\r/g, "").split("\n");
  let code = null,
    list = null,
    paragraph = [];
  const flush = () => {
    if (paragraph.length) {
      out.append(node("p", paragraph.join("\n")));
      paragraph = [];
    }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flush();
      list = null;
      if (code) {
        out.append(node("pre", code.join("\n")));
        code = null;
      } else code = [];
      continue;
    }
    if (code) {
      code.push(line);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    if (heading) {
      flush();
      list = null;
      out.append(node("h" + heading[1].length, heading[2]));
      continue;
    }
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])
    ) {
      flush();
      list = null;
      const table = node("table");
      const cells = (s) =>
        s
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((c) => c.trim());
      const row = (s, tag) => {
        const r = node("tr");
        for (const c of cells(s)) r.append(node(tag, c));
        table.append(r);
      };
      row(line, "th");
      i++;
      while (i + 1 < lines.length && lines[i + 1].includes("|")) {
        row(lines[++i], "td");
      }
      out.append(table);
      continue;
    }
    const item = line.match(/^\s*[-*+]\s+(.+)/);
    if (item) {
      flush();
      if (!list) {
        list = node("ul");
        out.append(list);
      }
      list.append(node("li", item[1]));
      continue;
    }
    list = null;
    if (!line.trim()) {
      flush();
      continue;
    }
    if (line.startsWith("> ")) {
      flush();
      out.append(node("blockquote", line.slice(2)));
      continue;
    }
    paragraph.push(line);
  }
  flush();
  if (code) out.append(node("pre", code.join("\n")));
  return out;
}
async function preview(file) {
  if (previewRequest) previewRequest.abort();
  const request = new AbortController();
  previewRequest = request;
  const url = fileUrl(file),
    ext = extension(file.name),
    content = $("preview-content");
  $("preview-title").textContent = file.name;
  $("preview-meta").textContent =
    file.project + " · " + date(file.published_at) + " · " + size(file.bytes);
  $("preview-open").href = url;
  $("preview-download").href = url;
  $("preview-download").download = file.name.split("/").pop();
  content.replaceChildren();
  if (!$("preview").open) $("preview").showModal();
  if (imageExtensions.has(ext)) {
    const img = node("img");
    img.src = url;
    img.alt = file.name;
    content.append(img);
    return;
  }
  if (ext === "html") {
    content.append(
      node("p", "这是网页报告。点击下方“打开原文件”查看交互内容。"),
    );
    return;
  }
  if (!textExtensions.has(ext) || file.bytes > 1048576) {
    content.append(
      node(
        "p",
        "此文件可通过下方链接打开或下载。文本预览支持 1 MB 以内的文件。",
      ),
    );
    return;
  }
  content.append(node("p", "正在读取…"));
  try {
    const response = await fetch(url, { signal: request.signal });
    if (!response.ok) throw Error("unavailable");
    const reader = response.body.getReader();
    let length = 0;
    const chunks = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > 1048576) {
        await reader.cancel();
        throw Error("large");
      }
      chunks.push(value);
    }
    const all = new Uint8Array(length);
    let pos = 0;
    for (const chunk of chunks) {
      all.set(chunk, pos);
      pos += chunk.length;
    }
    let text = new TextDecoder().decode(all);
    if (previewRequest !== request) return;
    if (ext === "json") {
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch {}
    }
    content.replaceChildren(
      ["md", "markdown"].includes(ext) ? markdown(text) : node("pre", text),
    );
  } catch (e) {
    if (e.name !== "AbortError" && previewRequest === request)
      content.replaceChildren(
        node("p", "无法预览此文件，请尝试打开原文件或下载。"),
      );
  }
}
async function load() {
  $("refresh").disabled = true;
  try {
    const response = await fetch("library.json", { cache: "no-store" });
    if (!response.ok) throw Error("unavailable");
    const data = await response.json();
    if (data.schema !== "result-sharing/v1" || !Array.isArray(data.projects))
      throw Error("invalid");
    projects = data.projects;
    records = [];
    for (const p of projects)
      for (const r of p.releases)
        for (const [name, info] of Object.entries(r.files))
          records.push({
            ...info,
            project: p.project,
            revision: r.revision,
            published_at: r.published_at,
            title: r.title,
            name,
          });
    render();
  } catch {
    $("status").textContent = "结果目录暂时不可用，请点击刷新重试。";
  } finally {
    $("refresh").disabled = false;
  }
}
$("search").oninput = (e) => {
  query = e.target.value.trim().toLocaleLowerCase();
  render();
};
$("type").onchange = (e) => {
  kind = e.target.value;
  render();
};
$("sort").onchange = (e) => {
  order = e.target.value;
  render();
};
$("version").onchange = (e) => {
  const s = state();
  location.hash = route(s.project, "", e.target.value);
};
$("refresh").onclick = load;
$("close-preview").onclick = () => $("preview").close();
$("preview").addEventListener("close", () => {
  if (previewRequest) previewRequest.abort();
  previewRequest = null;
});
addEventListener("hashchange", render);
load();
