// Local Lucide symbol subset; see ICONS-LICENSE.txt. No user-authored SVG.
const names = new Set([
  "layout-dashboard",
  "messages-square",
  "book-open",
  "terminal",
  "activity",
  "user-round",
  "cpu",
  "folder",
  "file-text",
  "brain",
  "arrow-up-right",
  "search",
]);
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ui-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#icon-${names.has(name) ? name : "file-text"}`);
  svg.append(use);
  return svg;
}
