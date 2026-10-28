import { ui, bindText, writeText } from "./i18n.js";
import { node, action, errorBox } from "./components.js";
import { objectDetail, cardsPage, section } from "./details.js";
import {
  memoryDirectory,
  memoryFile,
  character,
  state,
} from "./memory-character.js";
import { get } from "./components.js";
import { timeline } from "./monitor.js";
import { modelRounds } from "./model-rounds.js";
const dialog = document.getElementById("inspector"),
  body = document.getElementById("inspector-body");
const kinds = new Set([
  "threads",
  "episodes",
  "runs",
  "jobs",
  "file",
  "directory",
  "character",
  "state",
  "collection",
  "runtime",
  "activity",
  "rounds",
]);
let path = [],
  requestVersion = 0;
function hashFor(items) {
  return (
    "#inspect/" +
    items.map(([kind, id]) => `${kind}/${encodeURIComponent(id)}`).join("/")
  );
}
export function openObject(kind, id, root = false) {
  const next = root ? [] : path.slice();
  if (next.at(-1)?.[0] === kind && next.at(-1)?.[1] === id) return;
  next.push([kind, id]);
  location.hash = hashFor(next.slice(-12));
}
function close() {
  // Stay on the current section page and do not jump to an anchor.
  history.replaceState(null, "", location.pathname + location.search);
  route();
}
document.getElementById("drawer-close").addEventListener("click", close);
dialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  close();
});
dialog.addEventListener("click", (event) => {
  if (
    event.target === dialog &&
    event.clientX < dialog.getBoundingClientRect().left
  )
    close();
});
async function route() {
  const version = ++requestVersion;
  if (!location.hash.startsWith("#inspect/")) {
    path = [];
    if (dialog.open) dialog.close();
    return;
  }
  const [hash, query] = location.hash.split("?"),
    parts = hash.slice(9).split("/");
  path = [];
  try {
    for (let i = 0; i < parts.length; i += 2) {
      if (!kinds.has(parts[i]) || !parts[i + 1]) throw Error();
      path.push([parts[i], decodeURIComponent(parts[i + 1])]);
    }
  } catch {
    close();
    return;
  }
  if (!path.length || path.length > 12) {
    close();
    return;
  }
  dialog.classList.toggle("full", query === "full=1");
  if (!dialog.open) dialog.showModal();
  document.getElementById("full-view").href = hash + (query ? "" : "?full=1");
  bindText(
    document.getElementById("full-view"),
    query ? "Drawer view ↙" : "Full detail view ↗",
  );
  const crumbs = document.getElementById("breadcrumbs");
  crumbs.replaceChildren(action(ui("Workspace"), close));
  path.forEach(([kind, id], index) => {
    crumbs.append(
      node("span", "", "/"),
      action(
        kind === "file" || kind === "directory"
          ? id.split("/").at(-1)
          : ui(kind),
        () => {
          location.hash = hashFor(path.slice(0, index + 1));
        },
      ),
    );
  });
  const [kind, id] = path.at(-1);
  bindText(document.getElementById("inspector-kind"), kind);
  bindText(document.getElementById("inspector-title"), "Loading…");
  body.replaceChildren(node("p", "empty loading", ui("Reading local source…")));
  const result = node("div");
  let title;
  try {
    if (kind === "runtime") {
      title = ui("Runtime diagnostics");
      result.append(
        node(
          "pre",
          "record",
          JSON.stringify(await get("/api/status"), null, 2),
        ),
      );
    } else if (kind === "activity") {
      title = ui("Earlier activity");
      await timeline(openObject, id, result);
    } else if (kind === "directory") {
      await memoryDirectory(result, id === "root" ? "" : id, openObject);
      title = id === "root" ? ui("Memory files") : id;
    } else if (kind === "file")
      title = await memoryFile(result, id, openObject);
    else if (kind === "character") title = await character(result, openObject);
    else if (kind === "state") {
      await state(result, openObject);
      title = ui("Character state");
    } else if (kind === "collection") {
      if (!["runs", "episodes", "threads", "jobs"].includes(id))
        throw Error("Unknown collection");
      title = ui(id);
      section(result, `All ${id} / newest created first`);
      await cardsPage(
        result,
        `/api/${id === "jobs" ? "learning/jobs" : id}?limit=8`,
        id,
        openObject,
      );
    } else if (kind === "rounds") {
      title = ui("Model rounds & prompts");
      await modelRounds(result, id, openObject);
    } else title = await objectDetail(kind, id, result, openObject);
    if (version !== requestVersion) return;
    writeText(document.getElementById("inspector-title"), title);
    body.replaceChildren(result);
    dialog.scrollTop = 0;
  } catch (error) {
    if (version === requestVersion) {
      bindText(document.getElementById("inspector-title"), "Unavailable");
      body.replaceChildren(errorBox(error.message));
    }
  }
}
window.addEventListener("hashchange", route);
route();
