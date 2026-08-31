// Mobile nav collapse: the hamburger button and its target panel only
// render as a toggle below the .nav-toggle breakpoint in style.css —
// above it, .nav-collapse is always visible and this is inert.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("nav-toggle");
    var panel = document.getElementById("nav-collapse");
    if (!btn || !panel) return;

    function setOpen(open) {
      panel.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }

    btn.addEventListener("click", function () {
      setOpen(!panel.classList.contains("open"));
    });

    // Tapping a nav link navigates away anyway, but closing first avoids
    // a flash of the open menu on browsers that cache the previous DOM
    // state when navigating back.
    panel.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () { setOpen(false); });
    });
  });
})();
