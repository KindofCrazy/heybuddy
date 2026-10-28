// Runs before rendering. Existing localStorage preferences migrate without a flash.
(() => {
  const root = document.documentElement;
  let language = root.lang;
  try {
    const saved = localStorage.getItem("heybuddy.observatory.language");
    if (saved === "en" || saved === "zh-CN") language = saved;
  } catch {}
  if (language !== root.lang) root.dataset.languagePending = "";
  root.lang = language;
  document.cookie = `heybuddy_language=${language}; Path=/; Max-Age=31536000; SameSite=Strict`;
})();
