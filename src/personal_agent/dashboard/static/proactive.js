import {
  node,
  get,
  badge,
  empty,
  action,
  errorBox,
  time,
} from "./components.js";
import { ui } from "./i18n.js";

export async function proactivePage() {
  const names = {
    proposals: "Opportunities",
    decisions: "Decisions",
    outbox: "Message drafts",
    attempts: "Simulation attempts",
  };
  const tabs = node("nav", "proactive-tabs");
  const panels = Object.keys(names);
  function select(kind) {
    for (const button of tabs.children) {
      const active = button.dataset.panel === kind;
      button.setAttribute("aria-pressed", String(active));
      document.getElementById(button.dataset.panel).hidden = !active;
    }
  }
  for (const kind of panels) {
    const button = action(ui(names[kind]), () => select(kind));
    button.dataset.panel = kind;
    button.setAttribute("aria-controls", kind);
    tabs.append(button);
  }
  document.getElementById("proposals").before(tabs);
  select("proposals");
  const status = document.getElementById("content-proactive-status");
  try {
    const value = await get("/api/proactive/status");
    status.replaceChildren(
      badge(value.enabled ? "Shadow Mode" : "disabled"),
      node("p", "muted", ui(value.gate || "disabled")),
      node(
        "p",
        "",
        ui("Observes opportunities and decisions; sends no messages or tasks."),
      ),
    );
  } catch {
    status.replaceChildren(errorBox());
  }
  for (const kind of ["proposals", "decisions", "outbox", "attempts"]) {
    const target = document.getElementById(`content-${kind}`);
    const list = node("div", "stack");
    target.replaceChildren(list);
    async function load(cursor = null) {
      try {
        const data = await get(
          `/api/proactive/${kind}?limit=12${cursor ? `&cursor=${cursor}` : ""}`,
        );
        if (!data.items.length && !cursor) list.append(empty("No records yet"));
        for (const item of data.items) {
          const entry = node("article", "object-card");
          entry.append(
            node("h3", "", item.title || ui(item.action || item.status)),
            badge(item.status || item.action),
            node(
              "p",
              "",
              item.context || item.reason || item.text || item.error_code || "",
            ),
            node("p", "muted", item.mode || ""),
            node("p", "muted", time(item.updated_at || item.created_at)),
          );
          if (item.next_review_at)
            entry.append(node("p", "muted", time(item.next_review_at)));
          const detail = node("details");
          detail.append(
            node("summary", "", ui("Evidence and decision")),
            node(
              "pre",
              "",
              JSON.stringify(item.sources || item.payload || item, null, 2),
            ),
          );
          entry.append(detail);
          list.append(entry);
        }
        if (data.has_more) {
          const more = action(ui("Load more"), async () => {
            more.remove();
            await load(data.next_cursor);
          });
          list.append(more);
        }
      } catch {
        list.append(errorBox());
      }
    }
    await load();
  }
}
