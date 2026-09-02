/*
 * Copyright DB InfraGO AG and contributors
 * SPDX-License-Identifier: Apache-2.0
 */

import "bigger-picture/dist/bigger-picture.css";
import BiggerPicture from "bigger-picture/dist/bigger-picture.mjs";

import "htmx.org";
import "./htmx.js";
import "idiomorph/dist/idiomorph.js";
import "idiomorph/dist/idiomorph-ext.js";

import {
  Chart,
  ArcElement,
  PieController,
  Legend,
  Tooltip,
  Title,
} from "chart.js";
Chart.register(ArcElement, PieController, Legend, Tooltip, Title);
window.Chart = Chart;

import "./compiled.css";

window.openDiagramViewer = function (svgContainer) {
  if (typeof window.lightbox === "undefined") {
    window.lightbox = BiggerPicture({ target: document.body });
  }
  lightbox.open({ items: [svgContainer], el: svgContainer });
};

window.toggleToc = function () {
  const toc = document.getElementById("table-of-contents");
  if (!toc) return;

  const isOpen = !toc.classList.contains("translate-x-full");
  toc.classList.toggle("translate-x-full", isOpen);
  toc.classList.toggle("translate-x-0", !isOpen);
};

const xlMediaQuery = window.matchMedia("(min-width: 1280px)");
function resetTocOnResize() {
  const toc = document.getElementById("table-of-contents");
  if (!toc) return;

  toc.classList.toggle("translate-x-full", !xlMediaQuery.matches);
  toc.classList.toggle("translate-x-0", xlMediaQuery.matches);
}

window.addEventListener("resize", resetTocOnResize);
document.addEventListener("DOMContentLoaded", resetTocOnResize);

const reqStateChartInstances = new Map();

function initRequirementStateCharts(root) {
  const canvases = root.querySelectorAll
    ? root.querySelectorAll("canvas.req-state-chart")
    : [];
  canvases.forEach((canvas) => {
    if (reqStateChartInstances.has(canvas.id)) {
      reqStateChartInstances.get(canvas.id).destroy();
      reqStateChartInstances.delete(canvas.id);
    }

    let labels, values;
    try {
      labels = JSON.parse(canvas.dataset.labels);
      values = JSON.parse(canvas.dataset.values);
    } catch (err) {
      console.error("Failed to parse requirement chart data", err);
      return;
    }

    try {
      const chart = new window.Chart(canvas, {
        type: "pie",
        data: {
          labels,
          datasets: [
            {
              data: values,
              backgroundColor: [
                "#4caf50",
                "#888888",
                "#f44336",
                "#ff9800",
                "#2196f3",
                "#9c27b0",
                "#795548",
              ],
            },
          ],
        },
        options: {
          plugins: {
            legend: { position: "right" },
            title: { display: true, text: "Requirement status distribution" },
          },
        },
      });
      reqStateChartInstances.set(canvas.id, chart);
    } catch (err) {
      console.error("Failed to render requirement chart", err);
    }
  });
}

document.addEventListener("DOMContentLoaded", () =>
  initRequirementStateCharts(document),
);
document.addEventListener("htmx:afterSwap", (evt) =>
  initRequirementStateCharts(evt.target),
);
document.addEventListener("htmx:oobAfterSwap", (evt) =>
  initRequirementStateCharts(evt.target),
);
document.addEventListener("htmx:afterSettle", (evt) =>
  initRequirementStateCharts(evt.target),
);
