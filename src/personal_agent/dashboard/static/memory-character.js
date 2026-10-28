import { ui } from "./i18n.js";
import {
  node,
  get,
  action,
  badge,
  empty,
  errorBox,
  metadata,
  short,
} from "./components.js";
import { section, prose, cardsPage } from "./details.js";
export async function memoryDirectory(target, directory, open, cursor = "") {
  const data = await get(
    `/api/memory?directory=${encodeURIComponent(directory)}&limit=8&cursor=${encodeURIComponent(cursor)}`,
  );
  if (!data.items.length)
    target.append(
      empty(
        "No legal Markdown files",
        "MemoryFS may be empty; private or invalid paths are not listed.",
      ),
    );
  for (const entry of data.items) {
    const button = action(
      "",
      () => open(entry.kind === "directory" ? "directory" : "file", entry.path),
      "file-row",
    );
    button.append(
      node("span", "file-icon", entry.kind === "directory" ? "↳" : "md"),
      node("strong", "", entry.name),
      node("span", "mono", "↗"),
    );
    target.append(button);
  }
  if (data.has_more) {
    const more = action(
      ui("More files ↓"),
      async () => {
        more.disabled = true;
        try {
          await memoryDirectory(target, directory, open, data.next_cursor);
          more.remove();
        } catch (error) {
          more.disabled = false;
          target.append(errorBox(error.message));
        }
      },
      "more-button",
    );
    target.append(more);
  }
}
export async function memoryFile(target, path, open, offset = 0, hash = null) {
  const data = await get(
    `/api/memory/file?path=${encodeURIComponent(path)}&offset=${offset}${hash ? "&expected_hash=" + encodeURIComponent(hash) : ""}`,
  );
  if (!offset) {
    target.append(
      metadata(data, ["path", "updated_at", "hash"]),
      node("p", "notice", ui(data.history)),
    );
    section(target, "Markdown content");
  }
  target.append(prose(data.html));
  if (!offset && data.is_empty)
    target.append(
      empty(
        "No notes in this file yet",
        "Browse the core files or topic folders on the left. Learning records appear when available.",
      ),
    );
  if (data.next_offset !== null) {
    const more = action(
      ui("Continue reading ↓"),
      async () => {
        more.disabled = true;
        try {
          await memoryFile(target, path, open, data.next_offset, data.hash);
          more.remove();
        } catch (error) {
          more.disabled = false;
          target.append(errorBox(error.message));
        }
      },
      "more-button",
    );
    target.append(more);
  }
  if (!offset) {
    section(target, "Learning evidence / available update records");
    await cardsPage(
      target,
      `/api/memory/records?path=${encodeURIComponent(path)}&limit=6`,
      "jobs",
      open,
    );
  }
  return path;
}
export async function character(target, open) {
  const profile = await get("/api/character");
  if (!profile.enabled) {
    target.append(
      empty(
        "No active character package",
        "Character is disabled by Host configuration.",
      ),
    );
    return ui("Character");
  }
  target.append(
    badge("STABLE PROFILE"),
    metadata(profile, ["character_id", "fingerprint"]),
    prose(profile.profile_html),
  );
  const behavior = node("details", "message");
  behavior.append(
    node("summary", "", ui("Character behavior")),
    prose(profile.behavior_html),
  );
  target.append(behavior);
  section(target, "Dynamic simulated state");
  await state(target, open);
  return profile.character_id;
}
export async function state(target, open, cursor = null) {
  const data = await get(
    "/api/character/state?limit=8" + (cursor ? `&cursor=${cursor}` : ""),
  );
  if (!cursor) {
    if (data.active) {
      target.append(
        metadata(data.active, [
          "mood",
          "current_focus",
          "stance",
          "reason",
          "revision",
          "observed_at",
          "age_seconds",
        ]),
        action(
          ui(`Source Run ↗ ${short(data.active.source_run_id)}`),
          () => open("runs", data.active.source_run_id),
          "action-button",
        ),
      );
    } else
      target.append(
        empty(
          data.enabled ? "No active state" : "State disabled",
          "No state has formed, or its short-term TTL has expired.",
        ),
      );
    if (data.stored && !data.active) {
      const detail = node("details", "message");
      detail.append(
        node("summary", "", ui("Last stored state (inactive)")),
        metadata(data.stored, [
          "mood",
          "current_focus",
          "stance",
          "reason",
          "updated_at",
        ]),
        action(ui(`Source Run ↗ ${short(data.stored.source_run_id)}`), () =>
          open("runs", data.stored.source_run_id),
        ),
      );
      target.append(detail);
    }
    target.append(
      node(
        "p",
        "notice",
        ui(
          data.history_note ||
            "Simulated state is not evidence of subjective emotion.",
        ),
      ),
    );
    section(target, "State update / source-attempt receipts");
  }
  for (const receipt of data.items) {
    const row = action(
      "",
      () => open("episodes", receipt.episode_id),
      "nested",
    );
    row.append(
      badge(receipt.state),
      node(
        "p",
        "",
        ui(
          `Episode ${short(receipt.episode_id)} · REV ${receipt.episode_revision}`,
        ),
      ),
    );
    if (receipt.error) row.append(node("p", "muted", receipt.error));
    target.append(row);
  }
  if (!data.items.length) target.append(empty("No state update receipts"));
  if (data.has_more) {
    const more = action(
      ui("More state receipts ↓"),
      async () => {
        more.disabled = true;
        try {
          await state(target, open, data.next_cursor);
          more.remove();
        } catch (error) {
          more.disabled = false;
          target.append(errorBox(error.message));
        }
      },
      "more-button",
    );
    target.append(more);
  }
}
