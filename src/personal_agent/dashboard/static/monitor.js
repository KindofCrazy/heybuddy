import { icon } from "./icons.js";
import { ui, bindText } from "./i18n.js";
import {
  node,
  get,
  action,
  badge,
  empty,
  errorBox,
  time,
  short,
} from "./components.js";
const content = (id) => document.getElementById(`content-${id}`);
const signatures = new Map();
export function replace(target, data, ...children) {
  if (!target) return;
  const signature = JSON.stringify(data);
  if (
    signatures.get(target) === signature ||
    target.contains(document.activeElement)
  )
    return;
  signatures.set(target, signature);
  target.replaceChildren(...children);
}
export async function status(open) {
  try {
    const data = await get("/api/status"),
      diagnostic = data.diagnostics;
    const word = node("div", "status-word");
    word.append(
      node("span", `status-dot ${data.state === "Idle" ? "online" : ""}`),
      node("span", "", ui(data.state)),
    );
    const meta = node("dl", "meta-grid");
    const fields = [
      ["STAGE", data.current_stage || "No active stage"],
      ["LATEST RUN", data.latest_run?.status || "No runs yet"],
      ["QQ CHANNEL", data.qq.gateway],
      ["PENDING JOBS", diagnostic?.pending_jobs_capped ?? "Unavailable"],
    ];
    for (const [key, value] of fields) {
      const row = node("div");
      row.append(node("dt", "", ui(key)), node("dd", "", ui(value)));
      meta.append(row);
    }
    replace(
      content("status"),
      data,
      badge("HOST OBSERVED"),
      word,
      meta,
      action(ui("Runtime diagnostics ↗"), () => open("runtime", "current")),
    );
    bindText(document.getElementById("connection"), "Connected");
    document.getElementById("connection-dot").classList.add("online");
    const diagnostics = document.getElementById("job-diagnostics");
    if (diagnostics) {
      const box = node("div", "muted");
      box.append(
        node(
          "p",
          "",
          ui(
            diagnostic?.maintenance_running
              ? `Executing: ${data.current_stage}`
              : diagnostic?.background_task_active
                ? "Waiting for idle / maintenance eligibility"
                : "No background task scheduled",
          ),
        ),
      );
      if (diagnostic)
        box.append(
          node(
            "p",
            "",
            ui(
              `Pending jobs ${diagnostic.pending_jobs_capped} · runs ${diagnostic.pending_runs_capped} · dirty threads ${diagnostic.dirty_threads_capped}`,
            ),
          ),
        );
      if (diagnostic?.schedule?.last_error)
        box.append(
          node(
            "p",
            "notice",
            ui(`Last cycle outcome: ${diagnostic.schedule.last_error}`),
          ),
        );
      if (diagnostic?.episode_progress?.error_code)
        box.append(
          node(
            "p",
            "notice",
            ui(`Organizer: ${diagnostic.episode_progress.error_code}`),
          ),
        );
      if (diagnostic?.curator)
        box.append(
          node(
            "p",
            "mono",
            ui(`Curator due: ${time(diagnostic.curator.curator_due_at)}`),
          ),
        );
      box.append(
        action(ui("Inspect runtime & schedule ↗"), () =>
          open("runtime", "current"),
        ),
      );
      replace(diagnostics, diagnostic, box);
    }
  } catch {
    replace(content("status"), { error: true }, errorBox());
    bindText(document.getElementById("connection"), "Disconnected");
    document.getElementById("connection-dot").classList.remove("online");
  }
}
function eventItem(event, open) {
  const row = action(
    "",
    () => open(event.related_kind, event.related_object_id),
    "activity-item",
  );
  const label = node("span");
  label.append(
    node("strong", "", ui(event.event_type)),
    node(
      "span",
      "mono",
      ui(`${short(event.related_object_id)} · ${time(event.timestamp)}`),
    ),
    node(
      "span",
      "mono",
      ui(
        `${event.origin === "live_runtime" ? "LIVE" : "STORED"} / ${event.timestamp_basis.replaceAll("_", " ")}`,
      ),
    ),
  );
  const marker = node("span", "activity-icon");
  marker.append(icon(event.origin === "live_runtime" ? "cpu" : "activity"));
  row.append(marker, label, badge(event.status));
  return row;
}
export async function timeline(open, cursor = "", target = null) {
  try {
    const data = await get(
        `/api/activity?limit=${Math.max(1, Math.min(8, Math.floor((innerWidth <= 1100 ? (innerHeight - 200) / 2 : innerHeight - 220) / (innerWidth <= 680 ? 135 : 90))))}&cursor=${encodeURIComponent(cursor)}`,
      ),
      box = node("div", "timeline");
    for (const event of data.items) box.append(eventItem(event, open));
    if (!data.items.length)
      box.append(
        empty(
          "No activity yet",
          "Persistent facts and live stage observations will appear here.",
        ),
      );
    if (data.has_more)
      box.append(
        action(
          ui("Earlier activity ↓"),
          () => open("activity", data.next_cursor),
          "more-button",
        ),
      );
    box.append(
      node(
        "p",
        "muted",
        ui("Bounded recent window · source timestamps are labelled."),
      ),
    );
    if (target) target.append(box);
    else replace(content("activity"), data, box);
  } catch {
    if (target) target.append(errorBox());
    else replace(content("activity"), { error: true }, errorBox());
  }
}
