import { ui, translate } from "./i18n.js";
import { node, get, action, empty, errorBox } from "./components.js";
import { icon } from "./icons.js";
import { memoryFile } from "./memory-character.js";

const core = {
  "MEMORY.md": "Memory index",
  "human.md": "User context",
  "persona.md": "Core persona",
};
export async function memoryWorkspace(target, open) {
  const browser = node("div", "memory-workspace");
  const navigation = node("div", "memory-navigation");
  const reader = node("article", "memory-reader");
  reader.setAttribute("aria-live", "polite");
  browser.append(navigation, reader);
  target.replaceChildren(browser);
  let selected = "",
    generation = 0;

  async function read(path) {
    const version = ++generation;
    selected = path;
    for (const button of navigation.querySelectorAll("[data-file]")) {
      const active = button.dataset.file === path;
      button.classList.toggle("selected", active);
      button.setAttribute("aria-pressed", String(active));
    }
    reader.replaceChildren(
      node("p", "empty loading", ui("Reading local source…")),
    );
    const result = node("div", "memory-document");
    const heading = node("div", "memory-reader-heading");
    heading.append(
      icon("file-text"),
      node("h3", "", path.split("/").at(-1)),
      action(ui("Open full details ↗"), () => open("file", path)),
    );
    result.append(heading, node("p", "memory-reader-path mono", path));
    try {
      await memoryFile(result, path, open);
    } catch (error) {
      result.append(errorBox(error.message));
    }
    if (version === generation) reader.replaceChildren(result);
  }
  const toolbar = node("div", "memory-nav-toolbar");
  toolbar.append(icon("folder"), node("strong", "", ui("Memory files")));
  const form = node("form", "archival-search");
  const input = node("input");
  input.type = "search";
  input.maxLength = 256;
  input.required = true;
  input.dataset.i18nAriaLabel = "Search archival memory";
  input.setAttribute("aria-label", translate("Search archival memory"));
  const submit = node("button", "action-button", ui("Search"));
  submit.type = "submit";
  form.append(input, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    const version = ++generation;
    submit.disabled = true;
    try {
      const data = await get(
        `/api/memory/search?query=${encodeURIComponent(query)}`,
      );
      if (version !== generation) return;
      const results = node("div", "archival-results");
      results.append(node("h3", "", ui("Search results")));
      for (const item of data.results) {
        const result = node("article", "nested");
        result.append(
          action(item.path, () => read(item.path)),
          node("p", "prose", item.content),
        );
        results.append(result);
      }
      if (!data.results.length)
        results.append(empty("No matching topic memories"));
      if (data.scope_limited)
        results.append(
          node(
            "p",
            "muted",
            ui("Search scope is limited; browse folders for more memories."),
          ),
        );
      reader.replaceChildren(results);
    } catch (error) {
      if (version === generation)
        reader.replaceChildren(errorBox(error.message));
    } finally {
      submit.disabled = false;
    }
  });
  const coreFiles = node("div", "memory-core-files");
  const archive = node("div", "memory-tree-children");
  const coreGroup = node("section", "memory-tree-group");
  coreGroup.hidden = true;
  coreGroup.append(node("h4", "", ui("Core Memory")), coreFiles);
  const archiveGroup = node("section", "memory-tree-group");
  archiveGroup.append(node("h4", "", ui("Archival Memory")), archive);
  navigation.append(toolbar, form, coreGroup, archiveGroup);

  function fileEntry(entry) {
    const button = action("", () => read(entry.path), "file-row memory-file");
    const description = node("span", "memory-file-label");
    description.append(node("strong", "", entry.name));
    if (Object.hasOwn(core, entry.path))
      description.append(node("small", "muted", ui(core[entry.path])));
    button.append(icon("file-text"), description);
    button.title = entry.path;
    button.dataset.file = entry.path;
    button.classList.toggle("selected", entry.path === selected);
    button.setAttribute("aria-pressed", String(entry.path === selected));
    return button;
  }

  function folderEntry(entry) {
    const folder = node("details", "memory-folder");
    folder.dataset.directory = entry.path;
    const summary = node("summary");
    summary.title = entry.path;
    summary.append(
      icon("folder"),
      node("span", "memory-folder-name", entry.name),
    );
    const children = node("div", "memory-tree-children");
    folder.append(summary, children);
    let loaded = false;
    folder.addEventListener("toggle", async () => {
      if (!folder.open || loaded) return;
      loaded = true;
      await directoryPage(entry.path, children);
    });
    return folder;
  }

  async function directoryPage(path, container, cursor = "") {
    const loading = node("p", "muted", ui("Reading local source…"));
    container.append(loading);
    try {
      const data = await get(
        `/api/memory?directory=${encodeURIComponent(path)}&limit=8&cursor=${encodeURIComponent(cursor)}`,
      );
      const entries = node("div", "memory-tree-page");
      for (const entry of data.items) {
        if (!path && Object.hasOwn(core, entry.path)) {
          coreFiles.append(fileEntry(entry));
          coreGroup.hidden = false;
        } else
          entries.append(
            entry.kind === "directory" ? folderEntry(entry) : fileEntry(entry),
          );
      }
      container.append(entries);
      if (data.has_more) {
        const more = action(
          ui("More files ↓"),
          async () => {
            more.remove();
            await directoryPage(path, container, data.next_cursor);
          },
          "more-button",
        );
        container.append(more);
      } else if (!container.querySelector(".memory-file, .memory-folder")) {
        container.append(
          node(
            "p",
            "muted memory-folder-empty",
            ui(path ? "This folder is empty" : "No archived memories yet"),
          ),
        );
      }
      if (!selected && generation === 0 && !path && !cursor) {
        const first =
          data.items.find((entry) => entry.path === "MEMORY.md") ||
          data.items.find((entry) => entry.kind === "file");
        if (first) await read(first.path);
        else
          reader.replaceChildren(
            empty(
              "Select a memory file",
              "Select a file to read its content and learning evidence.",
            ),
          );
      }
    } catch (error) {
      const failure = errorBox(error.message);
      failure.append(
        action(ui("Retry"), () => {
          failure.remove();
          directoryPage(path, container, cursor);
        }),
      );
      container.append(failure);
    } finally {
      loading.remove();
    }
  }
  await directoryPage("", archive);
}
