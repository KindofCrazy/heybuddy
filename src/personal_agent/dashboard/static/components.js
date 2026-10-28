// Untrusted text enters the DOM through textContent, never HTML interpolation.
export function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== null && text !== undefined) element.textContent = String(text);
  return element;
}
export function short(id) {
  return id ? String(id).slice(0, 8) : "—";
}
export function time(value) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
export function badge(state) {
  const tone = /completed|finalized|active|idle/.test(state)
    ? "good"
    : /fail|error|reconcil|interrupted/.test(state)
      ? "bad"
      : "wait";
  return node("span", `badge ${tone}`, state || "unknown");
}
export function empty(title, description = "") {
  const box = node("div", "empty");
  box.append(node("strong", "", title), node("p", "", description));
  return box;
}
export function errorBox(
  message = "Local data unavailable. Try again shortly.",
) {
  const box = empty("暂时无法读取 / Unavailable", message);
  box.classList.add("error");
  return box;
}
export async function get(path) {
  const response = await fetch(path, {
    cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok)
    throw new Error(
      response.status === 404
        ? "Object not found or unavailable in this Agent."
        : "Local query failed.",
    );
  return response.json();
}
export function action(label, callback, className = "text-button") {
  const button = node("button", className, label);
  button.type = "button";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    callback(event);
  });
  return button;
}
export function objectCard(kind, item, open) {
  const card = action(
    "",
    () => open(kind, item.id),
    `nested ${kind === "threads" ? "thread-accent" : ""}`,
  );
  card.append(
    badge(item.status || item.state || "recorded"),
    node(
      "h3",
      "",
      item.title ||
        `${kind === "runs" ? "Run" : item.kind || kind} / ${short(item.id)}`,
    ),
  );
  card.append(
    node(
      "span",
      "mono",
      time(
        item.ended_at ||
          item.updated_at ||
          item.last_active_at ||
          item.started_at,
      ),
    ),
    node(
      "span",
      "mono",
      `${short(item.id)} ${item.revision ? "· REV " + item.revision : ""} ↗`,
    ),
  );
  return card;
}
export function metadata(data, keys) {
  const list = node("dl", "metadata");
  for (const key of keys) {
    if (data[key] === undefined) continue;
    const row = node("div");
    row.append(
      node("dt", "", key.replaceAll("_", " ")),
      node("dd", "mono", data[key] ?? "Unavailable"),
    );
    list.append(row);
  }
  return list;
}
