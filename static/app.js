// Theme toggle
(function () {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const isDark = document.documentElement.classList.toggle("dark");
    localStorage.setItem("theme", isDark ? "dark" : "light");
    window.dispatchEvent(new CustomEvent("themechange", { detail: { dark: isDark } }));
  });
})();
