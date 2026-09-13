# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0
"""Renders the artifact folder(s) bound to a model element.

Supporting documentation for physical interfaces - wiring diagrams,
connector pinouts, mechanical drawings, 3D models, datasheets, analysis
notebooks - lives outside the Capella model, in folders that a discipline
team owns in their own repository. Each folder describes itself with an
``artifact.yaml`` manifest, and ``mbse-artifact-viewer`` renders it.

This module is everything Model Explorer contributes to that, and it is
deliberately little:

* resolving *which* folder belongs to a model element, via
  ``artifacts/index.yaml``;
* handing that folder to the renderer, with a file source backed by the
  model's own resource handler;
* serving the files a rendered page then asks the browser to fetch (see
  ``artifact_file`` in :mod:`.app`).

The renderer knows nothing about Capella - no uuid, no ``.aird``, no PVMT
property - which is what lets a discipline team run it standalone on
their own repo, with ``mav serve``, before anything is committed.

IMPORTANT: reads go through ``model.resources["\\x00"]``, whose root is
wherever the model was told its own root is - which is *not* always the
repository root. Point capellambse straight at an ``.aird`` and the root
narrows to that file's own directory, from which ``artifacts/`` is
unreachable. The model must be configured with an explicit ``path`` (the
repository root) and ``entrypoint``, as ``model.json`` does.
"""

from __future__ import annotations

import logging
import mimetypes
import typing as t
import urllib.parse

import capellambse.model as m
import markupsafe
import yaml
from mbse_artifact_viewer import EMBED_STYLESHEET, paths, render_artifact

from capella_model_explorer import constants as c

logger = logging.getLogger(__name__)

# Not in the stdlib's table on every platform, and the 3D viewer asks for
# them by URL. Registered here because importing this module is what puts
# artifact serving in play.
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")
mimetypes.add_type("image/svg+xml", ".svg")

#: The model's root resource, per capellambse's own convention.
_ROOT_RESOURCE = "\x00"

#: Where the uuid -> folder table lives, relative to the model root.
INDEX_PATH = "artifacts/index.yaml"

#: Route that serves artifact files to the browser. Some content cannot
#: be inlined into a page - a browser will not open a PDF handed to it as
#: a data: URI, and a 3D model is fetched by the viewer - so those need a
#: real URL.
FILE_ROUTE = "artifact-file"


class _DuplicateReportingLoader(yaml.SafeLoader):
    """A YAML loader that complains about a repeated key.

    Binding one element to two artifacts is a supported thing to want,
    and the way to write it is a list. Writing it as two lines with the
    same uuid looks just as reasonable and quietly does something else:
    YAML keeps the last of duplicate keys, so the first artifact is
    dropped before anything in this application ever sees it, and the
    page renders one artifact with no hint that a second was asked for.

    PyYAML permits duplicates silently, so this says so out loud.
    """

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        seen: set[t.Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                logger.warning(
                    "%s: %s is listed more than once; YAML keeps only the "
                    "last, so the earlier artifact is ignored. Write the "
                    "several folders as a list under one key instead.",
                    INDEX_PATH,
                    key,
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


class ModelFileSource:
    """A ``mbse_artifact_viewer`` file source over the model's resources.

    The renderer asks for root-relative POSIX paths and nothing else,
    which is exactly what capellambse's handler takes. That indirection
    is the reason this works for a model fetched over HTTP or read out of
    a git object store, not only a local checkout.
    """

    def __init__(self, model: m.MelodyModel) -> None:
        self._handler = model.resources[_ROOT_RESOURCE]

    def read_bytes(self, path: str) -> bytes:
        return self._handler.read_file(path)

    def exists(self, path: str) -> bool:
        try:
            return self._handler.is_file(path)
        except Exception:
            # A handler may refuse a path outright rather than report it
            # missing - to the page, both mean "not there".
            return False

    def url_for(
        self, path: str, params: t.Mapping[str, str] | None = None
    ) -> str:
        url = f"{c.ROUTE_PREFIX}/{FILE_ROUTE}/" + "/".join(
            urllib.parse.quote(part) for part in path.split("/") if part
        )
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        return url


def folders_for(model: m.MelodyModel, uuid: str) -> list[str]:
    """The artifact folder(s) bound to ``uuid``, root-relative.

    ``artifacts/index.yaml`` is a flat mapping from uuid to folder, and a
    value may be a single folder or a list - one element can pull in more
    than one artifact (a harness diagram *and* a mechanical drawing for
    the same link).

    Paths there are relative to the index file's own directory, so they
    stay short for the common case and can still reach a sibling tree
    (``../analysis/ANA-001``) when an artifact lives outside
    ``artifacts/``.
    """
    handler = model.resources[_ROOT_RESOURCE]
    try:
        text = handler.read_file(INDEX_PATH).decode("utf-8")
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("Cannot read %s", INDEX_PATH)
        return []

    try:
        index = yaml.load(text, _DuplicateReportingLoader)
    except yaml.YAMLError:
        logger.exception("%s is not valid YAML", INDEX_PATH)
        return []

    if index is None:
        return []
    if not isinstance(index, dict):
        logger.warning(
            "%s must be a mapping of uuid to folder, not %s",
            INDEX_PATH,
            type(index).__name__,
        )
        return []

    value = index.get(uuid)
    if value is None:
        return []
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list):
        logger.warning(
            "%s: entry for %s must be a folder or a list of folders, not %s",
            INDEX_PATH,
            uuid,
            type(value).__name__,
        )
        return []

    base = paths.parent_of(INDEX_PATH)
    folders = []
    for entry in raw:
        try:
            folders.append(paths.join_relative(base, str(entry)))
        except Exception as err:
            logger.warning(
                "%s: %s is bound to an unusable folder %r: %s",
                INDEX_PATH,
                uuid,
                entry,
                err,
            )
    return folders


def render_artifacts(
    model: m.MelodyModel, obj: m.ModelElement | str | None
) -> markupsafe.Markup | None:
    """Render every artifact bound to ``obj``.

    Returns ``None`` when nothing is bound to it, so a template can leave
    the section out entirely rather than show an empty heading. Anything
    that *is* bound but cannot be rendered comes back as a visible note
    in its own place on the page - a declared artifact that isn't there
    has to be visible, not silently absent - and is logged as well.
    """
    if obj is None:
        return None
    uuid = obj if isinstance(obj, str) else obj.uuid
    folders = folders_for(model, uuid)
    if not folders:
        return None

    source = ModelFileSource(model)
    heads: dict[str, None] = {}
    bodies: list[str] = []

    for folder in folders:
        # Folded: a report embeds artifacts among other content, and
        # several of them at full height would bury the rest of the page.
        # The header line names each one, and "Collapse all" or a click on
        # a section opens what the reader actually wants.
        result = render_artifact(source, folder, collapsed=True)
        for diagnostic in result.diagnostics:
            logger.warning("Artifact %s: %s", uuid, diagnostic)
        if result.head:
            # Page-level markup (the 3D viewer's import map): once per
            # page however many artifacts ask for it.
            heads[result.head] = None
        bodies.append(result.html)

    # EMBED_STYLESHEET, not the package's full one. It carries structure -
    # frame sizes, borders, the notebook output blocks - and derives every
    # colour from `currentColor`, so the block inherits this page's font,
    # text colour and theme rather than bringing its own. The full sheet
    # declares a font and a palette, which is right for the renderer's own
    # standalone pages and wrong inside a page that has already chosen
    # both; it also switches theme on prefers-color-scheme, where Model
    # Explorer switches on a class, so the two would disagree whenever the
    # user's OS theme and the app's theme differ.
    return markupsafe.Markup(
        f"<style>{EMBED_STYLESHEET}</style>"
        + "".join(heads)
        + "\n".join(bodies)
    )


def has_artifacts(
    model: m.MelodyModel, obj: m.ModelElement | str | None
) -> bool:
    """Whether anything is bound to ``obj``, without rendering it."""
    if obj is None:
        return False
    uuid = obj if isinstance(obj, str) else obj.uuid
    return bool(folders_for(model, uuid))
