// Only explicitly labelled interface text is translated. Journal/Markdown stay raw.
const storageKey = "heybuddy.observatory.language";
let language = document.documentElement.lang === "en" ? "en" : "zh-CN";
try {
  if (localStorage.getItem(storageKey) === "en") language = "en";
} catch {}
const dictionary = await fetch("/static/zh-CN.json")
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));
const bindings = new WeakMap();
const english = await fetch("/static/en.json")
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));

export function locale() {
  return language;
}
export function translate(source) {
  source = String(source);
  if (language === "en") return english[source] || source;
  if (Object.hasOwn(dictionary, source)) return dictionary[source];
  const rules = [
    [/^No (.+) yet$/, (m) => `暂无${translate(m[1])}`],
    [/^Explore all (.+) ↗$/, (m) => `查看全部${translate(m[1])} ↗`],
    [
      /^All (.+) \/ newest created first$/,
      (m) => `全部${translate(m[1])} / 按创建时间倒序`,
    ],
    [/^Simulated mood: (.*)$/, (m) => `模拟心境：${m[1]}`],
    [/^Executing: (.*)$/, (m) => `正在执行：${translate(m[1])}`],
    [
      /^Pending jobs (\d+) · runs (\d+) · dirty threads (\d+)$/,
      (m) => `待处理任务 ${m[1]} · 待整理运行 ${m[2]} · 待更新主题 ${m[3]}`,
    ],
    [/^Last cycle outcome: (.*)$/, (m) => `最近维护结果：${m[1]}`],
    [/^Organizer: (.*)$/, (m) => `整理器：${m[1]}`],
    [/^Curator due: (.*)$/, (m) => `下次记忆整理：${m[1]}`],
    [
      /^(Source Run|Parent Thread|Linked memory|Evidence|Episode|Run) ↗ (.*)$/,
      (m) =>
        `${{ "Source Run": "来源运行", "Parent Thread": "所属主题", "Linked memory": "关联记忆", Evidence: "来源证据", Episode: "片段", Run: "运行" }[m[1]]} ↗ ${m[2].replace(" · REV ", " · 修订 ")}`,
    ],
    [/^Episode (.*) · REV (.*)$/, (m) => `片段 ${m[1]} · 修订 ${m[2]}`],
    [
      /^(Run|runs|threads|episodes|jobs|priority|batch|curator) \/ (.*)$/,
      (m) => `${translate(m[1] === "Run" ? "run" : m[1])} / ${m[2]}`,
    ],
    [
      /^#(\d+) · (USER|ASSISTANT|TOOL|SYSTEM)( · bounded preview)?$/,
      (m) =>
        `#${m[1]} · ${translate(m[2].toLowerCase())}${m[3] ? " · 有界预览" : ""}`,
    ],
    [
      /^(LIVE|STORED) \/ (.*)$/,
      (m) => `${translate(m[1])} / ${translate(m[2])}`,
    ],
    [/^(.*) · REV (\d+) ↗$/, (m) => `${m[1]} · 修订 ${m[2]} ↗`],
  ];
  for (const [pattern, format] of rules) {
    const match = source.match(pattern);
    if (match) return format(match);
  }
  const event = source.match(
    /^(.*)_(started|completed|failed|cancelled|updated)$/,
  );
  if (event) return `${translate(event[1])} · ${translate(event[2])}`;
  return source;
}
class InterfaceText {
  constructor(source) {
    this.source = String(source);
  }
}
export function ui(source) {
  return source instanceof InterfaceText ? source : new InterfaceText(source);
}
export function bindText(element, source) {
  source = source instanceof InterfaceText ? source.source : String(source);
  bindings.set(element, source);
  element.dataset.uiBound = "";
  element.textContent = translate(source);
  return element;
}
export function writeText(element, value) {
  if (value instanceof InterfaceText) bindText(element, value);
  else {
    bindings.delete(element);
    delete element.dataset.uiBound;
    element.textContent = String(value);
  }
}
function update() {
  document.documentElement.lang = language;
  delete document.documentElement.dataset.languagePending;
  document.title =
    language === "en" ? "HeyBuddy — Observatory" : "HeyBuddy — 观测台";
  for (const element of document.querySelectorAll("[data-i18n]"))
    element.textContent = translate(element.dataset.i18n);
  for (const element of document.querySelectorAll("[data-i18n-aria-label]"))
    element.setAttribute(
      "aria-label",
      translate(element.dataset.i18nAriaLabel),
    );
  for (const element of document.querySelectorAll("[data-ui-bound]")) {
    if (bindings.has(element))
      element.textContent = translate(bindings.get(element));
  }
  for (const button of document.querySelectorAll("[data-language-toggle]")) {
    button.textContent = language === "en" ? "中文" : "English";
    button.setAttribute(
      "aria-label",
      language === "en" ? "切换到中文" : "Switch to English",
    );
  }
}
export function setLanguage(value) {
  language = value === "en" ? "en" : "zh-CN";
  try {
    localStorage.setItem(storageKey, language);
  } catch {}
  document.cookie = `heybuddy_language=${language}; Path=/; Max-Age=31536000; SameSite=Strict`;
  update();
}
update();
for (const button of document.querySelectorAll("[data-language-toggle]")) {
  button.addEventListener("click", () =>
    setLanguage(language === "en" ? "zh-CN" : "en"),
  );
}
