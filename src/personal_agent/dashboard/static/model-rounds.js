import { ui } from "./i18n.js";
import {
  node,
  get,
  action,
  badge,
  empty,
  errorBox,
  metadata,
} from "./components.js";

function snapshot(target, id, number, part) {
  const detail = node("details", "message round-payload");
  detail.dataset.part = part;
  detail.append(
    node(
      "summary",
      "",
      ui(
        part === "prompt"
          ? "Input prompt & tool definitions"
          : "Model response",
      ),
    ),
  );
  let loaded = false;
  async function load(offset = 0) {
    const data = await get(
      `/api/runs/${encodeURIComponent(id)}/rounds/${number}/payload?part=${part}&offset=${offset}`,
    );
    if (data.state !== "saved") {
      detail.append(
        empty(
          data.state === "too_large"
            ? "Snapshot exceeded the storage limit"
            : "No response snapshot recorded",
        ),
      );
      return;
    }
    detail.append(node("pre", "record", data.content));
    if (data.has_more) {
      const more = action(
        ui("Continue snapshot ↓"),
        async () => {
          more.disabled = true;
          try {
            await load(data.next_offset);
            more.remove();
          } catch (error) {
            more.disabled = false;
            detail.append(errorBox(error.message));
          }
        },
        "more-button",
      );
      detail.append(more);
    }
  }
  detail.addEventListener("toggle", async () => {
    if (!detail.open || loaded) return;
    loaded = true;
    const loading = node("p", "loading", ui("Reading local source…"));
    detail.append(loading);
    try {
      await load();
    } catch (error) {
      loaded = false;
      detail.append(errorBox(error.message));
    } finally {
      loading.remove();
    }
  });
  target.append(detail);
}

export async function modelRounds(target, id, open, after = 0) {
  if (!after) {
    target.append(action(ui("Back to run ↗"), () => open("runs", id)));
    target.append(
      node(
        "p",
        "notice",
        ui(
          "Captured at request time: internal messages and tools. Provider wire data and reasoning are excluded.",
        ),
      ),
    );
  }
  const data = await get(
    `/api/runs/${encodeURIComponent(id)}/rounds?limit=6&after=${after}`,
  );
  if (!data.items.length && !after) {
    target.append(
      empty(
        "No recorded model rounds",
        "This run has no saved request snapshots. Older prompts cannot be reconstructed from current memory.",
      ),
    );
  }
  for (const item of data.items) {
    const box = node("section", "nested model-round");
    box.dataset.round = item.number;
    const heading = node("div", "round-heading");
    heading.append(
      node("h3", "", ui(`Model round ${item.number}`)),
      badge(item.state),
    );
    box.append(
      heading,
      metadata(item, [
        "started_at",
        "ended_at",
        "elapsed_ms",
        "message_count",
        "tool_count",
        "max_output_tokens",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
      ]),
    );
    snapshot(box, id, item.number, "prompt");
    snapshot(box, id, item.number, "response");
    target.append(box);
  }
  if (data.has_more) {
    const more = action(
      ui("Next model rounds ↓"),
      async () => {
        more.disabled = true;
        try {
          await modelRounds(target, id, open, data.next_cursor);
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
