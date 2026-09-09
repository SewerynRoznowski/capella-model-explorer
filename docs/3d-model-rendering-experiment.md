# 3D Model Rendering — Experiment Findings

## Goal

Check whether Model Explorer report pages could show real 3D component
geometry (e.g. a simplified mesh exported from a CAD tool such as Fusion
360), instead of just diagrams and tables. The eventual target is fetching
that 3D file from wherever the Capella model itself is stored (its
repository / resource folder), not from this app's own static assets.

## What was tested

A throwaway proof-of-concept currently lives in
`templates/20-System Analysis/system-definition.html.j2`, under
"Appendix B: 3D Model Rendering Test (Experimental)". It renders a plain
procedural box (0.2 × 0.1 × 0.1 m) via Three.js, loaded as an ES module
directly from a CDN (`unpkg.com/three@0.160.0`), with hand-rolled
drag-to-rotate, scroll-to-zoom, an origin/axes marker, and a hover
tooltip showing the box's dimensions.

This section should be treated as scratch/experimental and removed or
replaced once the real pipeline is designed - it doesn't load any file,
it only proves the rendering mechanics work in this app's page-delivery
model.

## Findings

- **WebGL/Three.js works fine embedded in a rendered report page.**
  Nothing in the render pipeline strips `<script>` tags: templates are
  autoescaped only for Jinja expressions (`{{ }}`), not for literal HTML
  written directly in the `.j2` file, and the TOC post-processing step
  (`process_html_with_toc` in `reports.py`) re-serializes the parsed DOM
  with `lxml_html.tostring`, which preserves script tags as-is. No CSP is
  configured, so CDN script/module loading isn't blocked either.

- **A direct ES module CDN import is enough for a quick test** - no
  changes to the real frontend build pipeline
  (`frontend/app.js` → pnpm/vite → `static/bundle/app.js`, the path
  Chart.js goes through for the requirement-state charts) were needed.
  For a *permanent* feature this should probably move into that bundle
  instead of a CDN import, the same way Chart.js is handled.

- **Import maps don't reliably work with dynamically-injected content.**
  Three.js's official `OrbitControls` addon
  (`examples/jsm/controls/OrbitControls.js`) internally does
  `import * as THREE from "three"` - a bare specifier, which requires a
  browser import map (`<script type="importmap">`) to resolve. This app's
  report content can be swapped into the DOM dynamically via htmx
  (`hx-swap`), and a `type="importmap"` tag inserted that way is *not*
  reliably picked up by the browser - it only reliably works when part of
  the page's initial static parse. Symptom:
  `TypeError: The specifier "three" was a bare specifier, but was not
  remapped to anything.`
  **Workaround used**: avoid any Three.js addon that has an internal bare
  `"three"` import; hand-roll drag/zoom/hover directly against the core
  `three.module.js` API, which is only ever imported via a full URL.
  If a real feature needs an addon later, either bundle it properly
  (build step, no bare-specifier problem) or vet it for bare imports
  first.

- **Helpers (e.g. `AxesHelper`) must be parented to the mesh, not the
  scene, to move with it.** `scene.add(helper)` keeps it fixed in world
  space; `mesh.add(helper)` makes it inherit the mesh's transform.

- **Dimension display is format-independent once loaded.** Any loader
  (glTF/OBJ/STL/...) ends up producing the same in-memory Three.js
  geometry, so `new THREE.Box3().setFromObject(mesh)` gives a bounding
  box regardless of source format. The current test hardcodes its size
  (since it's a procedural primitive) but this generalizes directly.
  The real gotcha is **units**, not display: CAD tools disagree on
  default units (mm vs m vs inch), so a real pipeline needs to carry that
  metadata through the conversion step or normalize it - otherwise
  displayed dimensions are numerically fine but in the wrong scale.

## Open questions / actual next steps (deferred)

The real target - fetching the 3D file from the Capella model's own
repository/resource storage, keyed to a specific model element - was
**not** attempted yet. It needs:

1. **A way to associate a 3D file with a model element.** Some
   naming/path convention, or a PVMT-style reference property (similar
   in spirit to how `constraints.py` already links auxiliary
   PVMT-managed data to model elements).
2. **A new backend route to serve it.** The browser-side loader
   (`GLTFLoader`/`OBJLoader`/`STLLoader`) needs an HTTP URL, not a
   filesystem path - this app already has the same shape of problem
   solved for diagrams (rendered dynamically via a route, not shipped as
   static files), so a new route like `/report/model-asset/{uuid}` that
   resolves and streams the file from the model's resource folder would
   follow that existing pattern.
3. **Path/access scoping.** Unlike the current CDN-hosted test asset,
   this would mean reading arbitrary files out of the mounted model
   directory - resolution needs to be scoped strictly to the model's own
   resource folder (no path traversal) once this is implemented for
   real.
4. **Conversion step.** Fusion 360 doesn't export glTF/GLB directly, so
   a real pipeline needs an intermediate step (e.g. export STEP/OBJ from
   Fusion, convert via Blender's Python API or `assimp`) to produce a
   web-friendly `.glb`. glTF/GLB is the recommended target format since
   it carries materials/scene structure, not just bare geometry (unlike
   plain OBJ/STL).
