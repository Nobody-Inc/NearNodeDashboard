// Near Node Dashboard: starts the page once the core and every module script have loaded.
"use strict";

applyModules();
(async () => {
  markRange();
  themeCharts();
  try { await loadConfig(); } catch {}
  buildCards();   // if the settings couldn't be loaded, build every module's charts
  await refresh();
  schedule();
})();
