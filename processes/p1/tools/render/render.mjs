// Render BPMN and DMN models to SVG and PNG with the bpmn-io viewers, the same rendering Camunda Modeler uses,
// so the pictures look like the models do in the Modeler, with no styling of our own.
//
//   cd tools/render && npm install && node render.mjs ../../models/to-be/*.bpmn ../../models/to-be/*.dmn
//
// Writes next to each model: <name>.svg and <name>.png (BPMN diagram or DRD), and for a DMN file one
// <name>.<decision id>.png per decision table. Needs Google Chrome (CHROME to point elsewhere).
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const asset = (p) => fs.readFileSync(require.resolve(p), "utf8");

const BPMN = { js: asset("bpmn-js/dist/bpmn-viewer.production.min.js"),
  css: ["bpmn-js/dist/assets/diagram-js.css", "bpmn-js/dist/assets/bpmn-js.css"].map(asset).join("\n") };
const DMN = { js: asset("dmn-js/dist/dmn-viewer.production.min.js"),
  css: ["dmn-js/dist/assets/diagram-js.css", "dmn-js/dist/assets/dmn-js-shared.css",
    "dmn-js/dist/assets/dmn-js-drd.css", "dmn-js/dist/assets/dmn-js-decision-table.css",
    "dmn-js/dist/assets/dmn-font/css/dmn-embedded.css"].map(asset).join("\n") };

const page_html = (lib) => `<!doctype html><html><head><meta charset="utf-8"><style>${lib.css}
  body { margin: 0; background: #fff; font-family: Arial, sans-serif; } #c { width: 4000px; height: 3000px; }
  </style><script>${lib.js}</script></head><body><div id="c"></div></body></html>`;

async function svgToPng(page, svg, out) {
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#fff">${svg}</body></html>`);
  const el = await page.$("svg");
  await el.screenshot({ path: out, omitBackground: false });
}

async function renderBpmn(browser, file) {
  const page = await browser.newPage();
  await page.setViewport({ width: 4000, height: 3000, deviceScaleFactor: 2 });
  await page.setContent(page_html(BPMN));
  const svg = await page.evaluate(async (xml) => {
    const viewer = new BpmnJS({ container: "#c" });
    await viewer.importXML(xml);
    return (await viewer.saveSVG()).svg;
  }, fs.readFileSync(file, "utf8"));
  const base = file.replace(/\.bpmn$/, "");
  fs.writeFileSync(base + ".svg", svg);
  await svgToPng(page, svg, base + ".png");
  await page.close();
  return [base + ".svg", base + ".png"];
}

async function renderDmn(browser, file) {
  const page = await browser.newPage();
  await page.setViewport({ width: 2400, height: 3000, deviceScaleFactor: 2 });
  await page.setContent(page_html(DMN));
  const xml = fs.readFileSync(file, "utf8");
  const { svg, decisions } = await page.evaluate(async (xml) => {
    window.viewer = new DmnJS({ container: "#c" });
    await viewer.importXML(xml);
    const drd = viewer.getViews().find((v) => v.type === "drd");
    await viewer.open(drd);
    const svg = (await viewer.getActiveViewer().saveSVG()).svg;
    const decisions = viewer.getViews().filter((v) => v.type === "decisionTable").map((v) => v.element.id);
    return { svg, decisions };
  }, xml);
  const base = file.replace(/\.dmn$/, "");
  const written = [base + ".svg", base + ".png"];
  fs.writeFileSync(base + ".svg", svg);
  for (const id of decisions) {
    await page.evaluate(async (id) => {
      await viewer.open(viewer.getViews().find((v) => v.element.id === id));
    }, id);
    // just the table and its header, without the viewer's "View DRD" button or the empty page below
    const clip = await page.evaluate(() => {
      document.querySelectorAll(".view-drd").forEach((e) => (e.style.display = "none"));
      const boxes = [".decision-table-properties", ".tjs-table"].map((q) => document.querySelector(q).getBoundingClientRect());
      const x = Math.min(...boxes.map((b) => b.left)), y = Math.min(...boxes.map((b) => b.top));
      return { x, y, width: Math.max(...boxes.map((b) => b.right)) - x + 1, height: Math.max(...boxes.map((b) => b.bottom)) - y + 1 };
    });
    const out = `${base}.${id}.png`;
    await page.screenshot({ path: out, clip });
    written.push(out);
  }
  await svgToPng(page, svg, base + ".png");
  await page.close();
  return written;
}

const files = process.argv.slice(2);
if (!files.length) {
  console.error("usage: node render.mjs <model.bpmn|model.dmn> ...");
  process.exit(2);
}
const browser = await puppeteer.launch({ executablePath: CHROME, headless: true });
try {
  for (const f of files) {
    if (f.endsWith(".processdoctor.bpmn")) continue; // same picture as the executable model
    const out = f.endsWith(".dmn") ? await renderDmn(browser, f) : await renderBpmn(browser, f);
    out.forEach((o) => console.log("wrote", path.relative(process.cwd(), o)));
  }
} finally {
  await browser.close();
}
