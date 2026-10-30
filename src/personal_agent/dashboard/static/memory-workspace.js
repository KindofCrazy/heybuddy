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
  let directory = "",
    selected = "",
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
    result.append(heading);
    try {
      await memoryFile(result, path, open);
    } catch (error) {
      result.append(errorBox(error.message));
    }
    if (version === generation) reader.replaceChildren(result);
  }
  async function directoryPage(path, cursor = "", append = false) {
    try {
      const data = await get(
        `/api/memory?directory=${encodeURIComponent(path)}&limit=8&cursor=${encodeURIComponent(cursor)}`,
      );
      if (!append) {
        directory = path;
        navigation.replaceChildren();
        const toolbar = node("div", "memory-nav-toolbar");
        toolbar.append(
          icon("folder"),
          node("strong", "", ui("Archival Memory")),
        );
        const form = node("form", "archival-search");
        const input = node("input");
        input.type = "search";
        input.maxLength = 256;
        input.required = true;
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
                  ui(
                    "Search scope is limited; browse folders for more memories.",
                  ),
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
        navigation.append(
          toolbar,
          form,
          node(
            "p",
            "muted",
            ui("Select a file to read its content and learning evidence."),
          ),
        );
        const crumbs = node("div", "memory-path");
        crumbs.append(action(ui("Memory root"), () => directoryPage("")));
        let parent = "";
        for (const segment of path.split("/").filter(Boolean)) {
          parent = parent ? `${parent}/${segment}` : segment;
          const destination = parent;
          crumbs.append(
            node("span", "", "/"),
            action(segment, () => directoryPage(destination)),
          );
        }
        navigation.append(crumbs);
      }
      if (!data.items.length)
        navigation.append(empty("No legal Markdown files"));
      for (const entry of data.items) {
        const button = action(
          "",
          () =>
            entry.kind === "directory"
              ? directoryPage(entry.path)
              : read(entry.path),
          "file-row memory-file",
        );
        const description = node("span", "memory-file-label");
        description.append(node("strong", "", entry.name));
        description.append(
          node(
            "small",
            "muted",
            ui(
              entry.kind === "directory"
                ? "Topic folder"
                : core[entry.path] || "Topic document",
            ),
          ),
        );
        button.append(
          icon(entry.kind === "directory" ? "folder" : "file-text"),
          description,
          icon("arrow-up-right"),
        );
        if (entry.kind === "file") {
          button.dataset.file = entry.path;
          button.classList.toggle("selected", entry.path === selected);
          button.setAttribute("aria-pressed", String(entry.path === selected));
        }
        navigation.append(button);
      }
      if (data.has_more) {
        const more = action(
          ui("More files ↓"),
          async () => {
            more.disabled = true;
            await directoryPage(directory, data.next_cursor, true);
            more.remove();
          },
          "more-button",
        );
        navigation.append(more);
      }
      if (!selected) {
        const first =
          data.items.find(
            (entry) => entry.name === "MEMORY.md" && entry.kind === "file",
          ) || data.items.find((entry) => entry.kind === "file");
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
      navigation.replaceChildren(errorBox(error.message));
    }
  }
  await directoryPage("");
}
