# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0
"""Renders physical-interface artifacts (WireViz harness/connector diagrams).

A :class:`~capellambse.metamodel.cs.PhysicalLink` or
:class:`~capellambse.metamodel.cs.PhysicalPort` declares what *kind* of
interface it is by having a managed PVMT group applied (e.g.
``Interfaces.Physical Links`` / ``Interfaces.Physical Ports``), with its
``Interface Type`` property set to one of a configured set of literals (e.g.
"electrical custom", "electrical standard", "mechanical custom",
"mechanical standard").

That type is looked up in ``artifacts/index.yaml`` - a single file, at the
root of the model's own repository, with one table per element kind
(``physical_links`` / ``physical_ports``), each mapping an interface-type
category to a ``{uuid: folder}`` table (an entry may also be
``{folder: ..., ...}`` if it carries extra metadata; only ``folder`` matters
here). The folder named there (also inside the model's repository, next to
the ``.aird``) holds the actual artifact:

- for a link, a ``harness.yaml`` WireViz source describing the cable itself.
- for a port, a ``connector.yaml`` defining just that one reusable connector
  (see ``connectors/*/connector.yaml``) - not a complete WireViz harness on
  its own, so it's wrapped in a minimal self-referencing ``connections:``
  block before rendering (WireViz silently drops a connector that isn't
  referenced in any connection set).

Both are rendered to SVG on every request rather than stored pre-rendered,
so the diagram can never drift from the source that produced it.

All model-relative files are read through the model's own root resource
handler (``model.resources["\x00"]``), the same abstraction capellambse
itself uses - so this works whether the model is a local checkout, a git
clone, or fetched remotely, not just a plain local path.

IMPORTANT: that resource handler's root is wherever the model was told its
own root is - which is *not* always the repository root. If the model is
configured by pointing straight at the ``.aird`` file (e.g.
``MelodyModel("architecture/Foo.aird")``), capellambse narrows the handler's
root to the ``.aird``'s own parent directory (``architecture/``), and reads
cannot escape that root to reach a sibling ``artifacts/`` folder - there is
no ``../`` here, by design, since that wouldn't mean anything for a git or
HTTP-backed handler. For ``artifacts/index.yaml`` to resolve, the model
must instead be configured with an explicit ``path`` (the repository root)
and ``entrypoint`` (e.g. ``"architecture/Foo.aird"``, relative to that
root) - see ``capellambse.loadinfo`` / the model's JSON spec.

The PVMT group/property names and the interface-type -> index-category
mapping are read from ``<TEMPLATES_DIR>/interfaces.config.json`` (see
:data:`_DEFAULT_CONFIG`), mirroring how ``constraints.py`` externalises its
own PVMT configuration.
"""

from __future__ import annotations

import functools
import json
import logging
import pathlib
import typing as t

import capellambse.model as m
import markupsafe
import yaml
from wireviz.wireviz import parse as wireviz_parse

from capella_model_explorer import constants as c
from capella_model_explorer.constraints import _enum_value

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "interfaces.config.json"

#: The model's root resource, per capellambse's own convention
#: (`model.resources["\x00"]` is the handler for the resource that was
#: passed as the model's entrypoint).
_ROOT_RESOURCE = "\x00"

_DEFAULT_CONFIG: dict[str, t.Any] = {
    "link_pvmt_group_name": "Interfaces.Physical Links",
    "port_pvmt_group_name": "Interfaces.Physical Ports",
    "property_names": {
        "interface_type": "Interface Type",
    },
    "interface_types": {},
    "display_labels": {},
    "artifacts_dir": "artifacts",
    "index_file": "index.yaml",
    "harness_file": "harness.yaml",
    "connector_file": "connector.yaml",
}


@functools.lru_cache(maxsize=1)
def _read_config_file(path: str, mtime: float) -> dict[str, t.Any]:
    del mtime  # part of the cache key only, to invalidate on edits
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_config() -> dict[str, t.Any]:
    """Load the interfaces config from the templates directory.

    Falls back to :data:`_DEFAULT_CONFIG` if ``interfaces.config.json``
    doesn't exist (yet) or fails to parse. Cached by the file's mtime, so
    edits to the file take effect on the next request, without restarting
    the server - same as ``constraints.py``'s own config.
    """
    path = pathlib.Path(c.TEMPLATES_DIR) / CONFIG_FILENAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _DEFAULT_CONFIG
    try:
        return _read_config_file(str(path), mtime)
    except (OSError, json.JSONDecodeError) as err:
        logger.warning("Failed to load %s, using defaults: %s", path, err)
        return _DEFAULT_CONFIG


def _get_declared_type(
    element: m.ModelElement, group_name: str
) -> str | None:
    """Read ``element``'s declared interface type from its PVMT.

    Returns the literal name of the configured ``interface_type``
    property inside ``group_name``, or ``None`` if the group isn't
    applied, the property isn't set, or its value is empty - all of
    which just mean "no declared type", not an error.
    """
    config = _load_config()
    try:
        group = element.applied_property_value_groups.by_name(group_name)
    except KeyError:
        return None

    prop_name = config["property_names"]["interface_type"]
    try:
        pv = group.property_values.by_name(prop_name)
    except KeyError:
        return None

    return _enum_value(pv) or None


def get_interface_type(link: m.ModelElement) -> str | None:
    """Read a ``PhysicalLink``'s declared interface type from its PVMT."""
    return _get_declared_type(link, _load_config()["link_pvmt_group_name"])


def get_port_interface_type(port: m.ModelElement) -> str | None:
    """Read a ``PhysicalPort``'s declared interface type from its PVMT."""
    return _get_declared_type(port, _load_config()["port_pvmt_group_name"])


def get_artifact_category(interface_type: str | None) -> str | None:
    """Map a declared interface type to its ``index.yaml`` category.

    Returns ``None`` if ``interface_type`` is unset, or isn't one of the
    literals configured in ``interface_types`` - callers should treat
    that as "no artifact lookup possible", not an error.
    """
    if not interface_type:
        return None
    return _load_config()["interface_types"].get(interface_type)


def get_display_label(interface_type: str | None) -> str | None:
    """Map a declared interface type to a human-readable display label.

    Falls back to the raw ``interface_type`` literal (e.g. "electrical
    custom") if its category has no configured label, or the type
    doesn't map to a category at all - so an unconfigured type still
    shows something, rather than nothing.
    """
    if not interface_type:
        return None
    category = get_artifact_category(interface_type)
    return _load_config()["display_labels"].get(category, interface_type)


def _read_model_yaml(model: m.MelodyModel, path: str) -> t.Any | None:
    """Read and parse a YAML file relative to the model's own root.

    Returns ``None`` if the file doesn't exist. Uses the model's root
    resource handler rather than the plain filesystem, so this works
    whether the model is a local checkout, a git clone, or fetched
    remotely.
    """
    handler = model.resources[_ROOT_RESOURCE]
    try:
        raw = handler.read_file(path)
    except FileNotFoundError:
        logger.debug(
            "%s not found under model resource root %r - if the model was"
            " loaded by pointing straight at the .aird, its resource root"
            " is that file's own parent directory, not the repository"
            " root, and reads cannot escape it to reach a sibling"
            " artifacts/ folder. Load the model with an explicit path"
            " (repo root) + entrypoint (relative .aird path) instead.",
            path,
            getattr(handler, "path", handler),
        )
        return None
    return yaml.safe_load(raw)


def _resolve_index_folder(
    model: m.MelodyModel, section: str, category: str | None, uuid: str
) -> str | None:
    """Look up ``index.yaml[section][category][uuid]``'s bound folder.

    An entry may be a plain folder string, or a ``{folder: ..., ...}``
    mapping carrying extra metadata alongside it - either way, only the
    folder matters here. Returns ``None`` at any step that doesn't
    resolve, including a missing/malformed index - all treated as
    "nothing to render", not an error.
    """
    if category is None:
        return None

    config = _load_config()
    index_path = f"{config['artifacts_dir']}/{config['index_file']}"
    index = _read_model_yaml(model, index_path)
    if not isinstance(index, dict):
        logger.debug("No usable %s found for model", index_path)
        return None

    table = (index.get(section) or {}).get(category)
    if not isinstance(table, dict):
        return None

    entry = table.get(uuid)
    if isinstance(entry, dict):
        folder = entry.get("folder")
    else:
        folder = entry
    if not folder:
        return None

    return f"{config['artifacts_dir']}/{folder}"


def resolve_artifact_path(
    model: m.MelodyModel, link: m.ModelElement
) -> str | None:
    """Resolve the harness artifact folder bound to ``link``, if any.

    Chains: PVMT interface type -> ``index.yaml``'s ``physical_links``
    category -> that category's table -> the folder listed for
    ``link.uuid``.
    """
    category = get_artifact_category(get_interface_type(link))
    return _resolve_index_folder(model, "physical_links", category, link.uuid)


def resolve_connector_path(
    model: m.MelodyModel, port: m.ModelElement
) -> str | None:
    """Resolve the connector artifact folder bound to ``port``, if any.

    Chains: PVMT interface type -> ``index.yaml``'s ``physical_ports``
    category -> that category's table -> the folder listed for
    ``port.uuid``.
    """
    category = get_artifact_category(get_port_interface_type(port))
    return _resolve_index_folder(model, "physical_ports", category, port.uuid)


def render_harness_diagram(
    model: m.MelodyModel, link: m.ModelElement
) -> markupsafe.Markup | None:
    """Render the WireViz harness diagram bound to ``link``, if any.

    The diagram is rendered fresh from the artifact's ``harness.yaml`` on
    every call rather than read from a pre-rendered file, so it can never
    drift from the source that produced it. Returns ``None`` if no
    artifact is resolved for this link, or its folder has no
    ``harness.yaml`` - callers should show a fallback in that case
    rather than treating it as an error.
    """
    config = _load_config()
    artifact_path = resolve_artifact_path(model, link)
    if artifact_path is None:
        return None

    harness_path = f"{artifact_path}/{config['harness_file']}"
    handler = model.resources[_ROOT_RESOURCE]
    try:
        yaml_text = handler.read_file(harness_path).decode("utf-8")
    except FileNotFoundError:
        logger.warning(
            "Link %s resolves to artifact %r, but it has no %s",
            link.uuid,
            artifact_path,
            config["harness_file"],
        )
        return None

    try:
        svg = wireviz_parse(yaml_text, return_types="svg")
    except Exception:
        logger.exception(
            "Failed to render WireViz harness for link %s from %s",
            link.uuid,
            harness_path,
        )
        return None

    return markupsafe.Markup(svg)


def _wrap_connector_for_standalone_render(
    connector_text: str, designator: str = "X1"
) -> str:
    """Wrap a bare connector-template YAML so WireViz will render it alone.

    A connector artifact only defines a reusable anchor (see
    ``connectors/*/connector.yaml``); it has no ``connectors:`` section of
    its own, and WireViz silently drops any connector that isn't
    referenced in a ``connections:`` block at all. This adds the smallest
    possible self-reference to keep it in the render - same approach as
    ``tools/render_connector.py`` in the model's own repository.

    By convention, the connector artifact's top-level key is the same
    name as its anchor (``name: &name``), so the first top-level key
    doubles as the alias to use here.
    """
    connector_data = yaml.safe_load(connector_text)
    if not isinstance(connector_data, dict) or not connector_data:
        raise ValueError("Connector artifact has no top-level mapping")

    template_name = next(iter(connector_data))
    pinlabels = connector_data[template_name].get("pinlabels") or []
    pins = list(range(1, len(pinlabels) + 1))

    wrapper = f"""
connectors:
  {designator}:
    <<: *{template_name}

connections:
  - - {designator}: {pins}
"""
    return connector_text + "\n" + wrapper


def render_connector_diagram(
    model: m.MelodyModel, port: m.ModelElement
) -> markupsafe.Markup | None:
    """Render the standalone WireViz connector diagram bound to ``port``.

    Renders fresh from the artifact's ``connector.yaml`` on every call,
    same as :func:`render_harness_diagram`. Returns ``None`` if no
    artifact is resolved for this port, its folder has no
    ``connector.yaml``, or that file doesn't parse as a bare connector
    template - callers should show a fallback in that case rather than
    treating it as an error.
    """
    config = _load_config()
    artifact_path = resolve_connector_path(model, port)
    if artifact_path is None:
        return None

    connector_path = f"{artifact_path}/{config['connector_file']}"
    handler = model.resources[_ROOT_RESOURCE]
    try:
        connector_text = handler.read_file(connector_path).decode("utf-8")
    except FileNotFoundError:
        logger.warning(
            "Port %s resolves to artifact %r, but it has no %s",
            port.uuid,
            artifact_path,
            config["connector_file"],
        )
        return None

    try:
        wrapped = _wrap_connector_for_standalone_render(connector_text)
        svg = wireviz_parse(wrapped, return_types="svg")
    except Exception:
        logger.exception(
            "Failed to render WireViz connector for port %s from %s",
            port.uuid,
            connector_path,
        )
        return None

    return markupsafe.Markup(svg)
