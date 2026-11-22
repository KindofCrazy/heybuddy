import {
  node,
  get,
  badge,
  empty,
  action,
  errorBox,
} from "./components.js";
import { ui, bindTimestamp } from "./i18n.js";

const states = {
  scheduled: "Waiting for due time",
  blocked: "Delivery blocked",
  sending: "Sending",
  accepted: "Channel accepted",
  failed: "Failed",
  uncertain: "Delivery unconfirmed",
  cancelled: "Cancelled",
  expired: "Expired",
  composing: "Composing draft",
  prepared: "Draft ready",
  compose_failed: "Draft generation failed",
  open: "Following",
  deferred: "Review later",
  resolved: "Resolved",
  dismissed: "Dismissed",
  notify: "Check-in suggested",
  silent: "Stay silent",
  defer: "Review later",
  propose_task: "Task suggested",
  covered: "Progress discussed",
  linked_thread: "Topic link updated",
  skipped: "Skipped",
};
const reasons = {
  source_processed: "Source checked",
  run_not_completed: "This conversation did not complete normally and was skipped.",
  stale_thread: "This topic version is outdated and was skipped.",
  thread_source_budget: "This topic exceeds the source budget and was skipped.",
  source_budget: "Source size exceeds the budget; scanning is paused.",
  user_progress_shared: "The scheduled check-in was withdrawn.",
  lifetime_ended: "The follow-up window has expired.",
  followup_covered: "The scheduled check-in was withdrawn.",
  draft_stale: "New input or changed evidence invalidated this draft.",
  restart_requires_recheck: "An old draft was withdrawn after restart.",
  delivery_blocked: "The delivery permission, source or rate limit check did not pass.",
  send_uncertain: "Delivery could not be confirmed. It will not be retried.",
  send_interrupted: "Delivery could not be confirmed. It will not be retried.",
  send_unconfirmed: "Delivery could not be confirmed. It will not be retried.",
  channel_unavailable: "The reminder channel was unavailable at the due time.",
  metadata_only: "Only the topic link changed; the follow-up evidence is unchanged.",
  active_capacity: "There are already enough active follow-up items; this candidate was skipped.",
  provider_failed: "The model request failed. See technical details for the error.",
};

function dateLine(label, value) {
  const line = node("p", "muted");
  line.append(node("span", "", ui(label)), " ", bindTimestamp(node("time"), value));
  return line;
}

function duration(label, seconds) {
  const line = node("p", "muted");
  const minutes = seconds > 0 && seconds % 60 === 0;
  line.append(
    node("span", "", ui(label)),
    ` ${minutes ? seconds / 60 : seconds} `,
    node("span", "", ui(minutes ? "minutes" : "seconds")),
  );
  return line;
}

function recordCard(kind, item) {
  const entry = node("article", "object-card");
  const scan = kind === "decisions" && item.action === "scouted";
  const skipped = scan && item.reason !== "source_processed";
  const covered = item.followup_state === "covered";
  const state = covered ? "Progress discussed"
    : scan ? (skipped ? "Skipped" : "Checked")
    : item.status === "accepted" && item.mode === "simulation" ? "Simulation accepted"
    : states[item.status || item.action] || item.status || item.action || "Recorded";
  const title = scan
    ? ui(item.payload?.lane === "thread_cursor" ? "Topic source checked" : "Conversation source checked")
    : item.title || item.text || ui(states[item.action] || (kind === "decisions" ? "Decision recorded" : "Delivery record"));
  const mark = badge(state);
  if (item.status === "accepted" || (scan && !skipped)) {
    mark.classList.remove("wait");
    mark.classList.add("good");
  }
  if (["failed", "uncertain"].includes(item.status || item.action)) {
    mark.classList.remove("wait");
    mark.classList.add("bad");
  }
  entry.append(node("h3", "", title));
  if (kind !== "decisions" || scan) entry.append(mark);
  if (scan) {
    entry.append(node("p", "", ui(skipped
      ? reasons[item.reason] || "This source was skipped. See details for the reason."
      : "Source processed; this entry does not mean a message was sent.")));
    const count = item.payload?.changes;
    if (!skipped && Number.isInteger(count)) {
      if (count === 0) entry.append(node("p", "", ui("No follow-up items were added or updated.")));
      else {
        const summary = node("p");
        summary.append(node("span", "", ui("Follow-up items added or updated:")), ` ${count}`);
        entry.append(summary);
      }
    }
  } else if (item.context || item.reason) {
    entry.append(node("p", "", item.context || (reasons[item.reason] ? ui(reasons[item.reason]) : item.reason)));
  }
  if (item.error_code) {
    entry.append(node("p", "", ui(reasons[item.error_code] || "See technical details for the failure reason.")));
  }
  if (item.mode === "napcat" || item.mode === "simulation") {
    entry.append(node("p", "muted", ui(item.mode === "napcat" ? "NapCat private chat" : "Local simulation")));
  }
  if (item.status === "accepted") {
    entry.append(node("p", "muted", ui(item.mode === "simulation"
      ? "This was a local simulation; no QQ message was sent."
      : "The channel accepted the message; this does not confirm it was read.")));
  }
  entry.append(dateLine("Recorded at:", item.created_at || item.started_at || item.updated_at));
  if (item.due_at) entry.append(dateLine("Scheduled for:", item.due_at));
  if (item.ended_at) entry.append(dateLine("Delivery finished at:", item.ended_at));
  if (item.next_review_at) entry.append(dateLine("Next review:", item.next_review_at));
  if (covered) entry.append(node("p", "muted", ui("The scheduled check-in was withdrawn.")));
  const detail = node("details");
  detail.append(
    node("summary", "", ui("Technical details and evidence")),
    node("pre", "", JSON.stringify(item, null, 2)),
  );
  entry.append(detail);
  return entry;
}

export async function proactivePage() {
  const names = {
    proposals: "Opportunities",
    decisions: "Scans and decisions",
    outbox: "Check-in drafts",
    attempts: "Check-in deliveries",
    reminders: "Explicit reminders",
  };
  const descriptions = {
    proposals: "Follow-up items found in conversations. An item is not a sent message.",
    decisions: "Scans advance one source at a time after the idle threshold. A scan may find no follow-up item.",
    outbox: "Drafts for discretionary check-ins. Explicit reminders are listed separately.",
    attempts: "Only discretionary check-in delivery attempts appear here. Find timed reminders under Explicit reminders.",
    reminders: "Requests such as remind me in 3 minutes appear here, including their schedule and actual delivery result.",
  };
  const tabs = node("nav", "proactive-tabs");
  tabs.setAttribute("aria-label", "Proactive records");
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
      badge(value.enabled ? (value.outbound_enabled ? "NapCat delivery" : "Shadow Mode") : "disabled"),
      node("p", "muted", ui(value.gate || "disabled")),
    );
    if (value.maintenance) {
      const settings = node("div", "proactive-settings");
      settings.append(
        duration("Idle threshold:", value.maintenance.idle_seconds),
        duration("Polling interval:", value.maintenance.check_seconds),
        duration("Maintenance cooldown:", value.maintenance.cooldown_seconds),
      );
      status.append(settings);
    }
    status.append(node("p", "muted", ui("The idle threshold starts background work; the maintenance cycle advances it. New input pauses it. Explicit reminders use their own timer.")));
    const available = value.reminders?.delivery_available;
    status.append(node("p", "muted", ui(available
      ? "Explicit reminder delivery is connected. Scheduled times are independent of the idle threshold."
      : "Explicit reminder delivery is currently unavailable. Records remain visible below.")));
  } catch {
    status.replaceChildren(errorBox());
  }
  for (const kind of panels) {
    const target = document.getElementById(`content-${kind}`);
    const list = node("div", "stack");
    target.replaceChildren(node("p", "muted", ui(descriptions[kind])), list);
    async function load(cursor = null) {
      try {
        const path = kind === "reminders" ? "/api/reminders" : `/api/proactive/${kind}`;
        const data = await get(`${path}?limit=12${cursor ? `&cursor=${cursor}` : ""}`);
        if (!data.items.length && !cursor) list.append(empty("No records yet"));
        for (const item of data.items) list.append(recordCard(kind, item));
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
