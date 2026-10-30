import { node, get, badge, empty, action, errorBox, time } from "./components.js";
import { ui } from "./i18n.js";

export async function proactivePage() {
  const status = document.getElementById("content-proactive-status");
  try {
    const value = await get("/api/proactive/status");
    status.replaceChildren(
      badge(value.enabled ? "Shadow Mode" : "disabled"),
      node("p", "muted", ui(value.gate || "disabled")),
      node("p", "", "Observes opportunities and decisions; sends no messages or tasks."),
    );
  } catch { status.replaceChildren(errorBox()); }
  for (const kind of ["proposals", "decisions"]) {
    const target = document.getElementById(`content-${kind}`);
    const list = node("div", "stack");
    target.replaceChildren(list);
    async function load(cursor = null) {
      try {
        const data = await get(`/api/proactive/${kind}?limit=12${cursor ? `&cursor=${cursor}` : ""}`);
        if (!data.items.length && !cursor) list.append(empty("No records yet"));
        for (const item of data.items) {
          const entry = node("article", "object-card");
          entry.append(
            node("h3", "", item.title || ui(item.action)),
            badge(item.status || item.action),
            node("p", "", item.context || item.reason),
            node("p", "muted", time(item.updated_at || item.created_at)),
          );
          if (item.next_review_at)
            entry.append(node("p", "muted", `${ui("Next review")}: ${time(item.next_review_at)}`));
          const detail = node("details");
          detail.append(node("summary", "", ui("Evidence and decision")),
            node("pre", "", JSON.stringify(item.sources || item.payload, null, 2)));
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
      } catch { list.append(errorBox()); }
    }
    await load();
  }
}
