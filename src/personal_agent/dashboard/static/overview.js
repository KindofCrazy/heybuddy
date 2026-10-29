import { ui } from "./i18n.js";
import {
  node,
  get,
  empty,
  errorBox,
  objectCard,
  action,
} from "./components.js";
import { openObject } from "./inspectors.js";
import { cardsPage } from "./details.js";
import { memoryWorkspace } from "./memory-workspace.js";
import { characterPortrait } from "./memory-character.js";
import { status, timeline, replace } from "./monitor.js";

const content = (id) => document.getElementById(`content-${id}`);
const open = (kind, id) => openObject(kind, id, true);
let profile = null;
const page = document.body.dataset.page || "overview";

async function collection(id, kind, limit, className) {
  const target = content(id);
  if (!target) return;
  try {
    const data = await get(
      `/api/${kind === "jobs" ? "learning/jobs" : kind}?limit=${limit}`,
    );
    const box = node("div");
    const grid = node("div", className);
    data.items.forEach((item) => grid.append(objectCard(kind, item, open)));
    if (!data.items.length)
      grid.append(empty(`No ${kind} yet`, "真实记录形成后，会出现在这里。"));
    box.append(
      grid,
      action(ui(`Explore all ${kind} ↗`), () => open("collection", kind)),
    );
    if (id === "threads")
      box.append(
        action(ui("All Episodes, including unassigned ↗"), () =>
          open("collection", "episodes"),
        ),
      );
    replace(target, data, box);
  } catch {
    replace(target, { error: true }, errorBox());
  }
}

async function character() {
  try {
    profile ??= await get("/api/character");
    const state = profile.enabled
      ? await get("/api/character/state?limit=1")
      : null;
    const box = node("div");
    if (!profile.enabled)
      box.append(
        empty("No active character package", "Character is disabled."),
      );
    else {
      const identity = node("div", "character-identity");
      const portrait = characterPortrait(profile);
      if (portrait) {
        const button = action(
          "",
          () => open("character", "current"),
          "portrait-button",
        );
        button.append(portrait);
        identity.append(button);
      }
      const description = node("div", "character-description");
      description.append(
        node(
          "h3",
          "",
          profile.character_id === "luna" ? "Luna" : profile.character_id,
        ),
      );
      description.append(
        node(
          "p",
          "notice",
          ui(
            state.active
              ? `Simulated mood: ${state.active.mood}`
              : "No active short-term state.",
          ),
        ),
      );
      identity.append(description);
      box.append(identity);
      if (state.active?.current_focus)
        box.append(node("p", "muted", state.active.current_focus));
    }
    box.append(
      action(ui("Explore profile & state ↗"), () =>
        open("character", "current"),
      ),
    );
    replace(content("character"), { profile, state }, box);
  } catch {
    replace(content("character"), { error: true }, errorBox());
  }
}

// Dedicated pages read their own bounded lists once; pagination is user-driven.
async function pageCollection(id, kind) {
  const target = content(id);
  if (!target) return;
  target.replaceChildren();
  try {
    await cardsPage(
      target,
      `/api/${kind === "jobs" ? "learning/jobs" : kind}?limit=${Math.max(3, Math.min(12, Math.floor((innerHeight - 180) / 90) * 3))}`,
      kind,
      open,
    );
  } catch {
    target.replaceChildren(errorBox());
  }
}
if (page === "memory" && content("memory"))
  await memoryWorkspace(content("memory"), open);
if (content("jobs")) {
  const jobs = node("div");
  jobs.id = "job-diagnostics";
  const jobList = node("div");
  jobList.id = "content-job-list";
  content("jobs").replaceChildren(jobs, jobList);
}
if (page === "threads")
  await Promise.allSettled([
    pageCollection("threads", "threads"),
    pageCollection("episodes", "episodes"),
  ]);
if (page === "runs") await pageCollection("runs", "runs");
if (page === "memory") await pageCollection("job-list", "jobs");

let refreshCount = 0;
async function refresh() {
  if (!document.hidden) {
    const work = [status(open)];
    if (page === "overview") {
      work.push(timeline(open));
      if (refreshCount % 5 === 0)
        work.push(
          collection(
            "job-list",
            "jobs",
            Math.max(
              1,
              Math.min(
                3,
                Math.floor(
                  ((innerWidth <= 1100
                    ? (innerHeight - 200) / 2
                    : innerHeight - 220) -
                    130) /
                    140,
                ),
              ),
            ),
            "stack",
          ),
          character(),
        );
    }
    await Promise.allSettled(work);
    refreshCount++;
  }
  window.setTimeout(refresh, 3000);
}
refresh();
