# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0
"""Helpers for Physical Architecture nesting and deployment.

Modelling convention this project follows: composition (``.parent`` /
``owned_components``) is used for *all* nesting - a board inside a box
inside a chassis, and equally a software layer inside another software
layer (e.g. an application behavior that is a *part of* a middleware
behavior, itself a *part of* an OS behavior). Deployment
(``deploying_components`` / ``deployed_components``) is reserved strictly
for the one boundary crossing from an outermost Behavior to the Node that
actually runs it. Capella itself allows deployment to be used for either
kind of nesting too, which is exactly the confusing "you can do this two
different ways" pitfall this convention avoids - do not use deployment for
Behavior-in-Behavior or Node-in-Node nesting.
"""

from __future__ import annotations

import capellambse.model as m


def _walk_parents_while(
    component: m.ModelElement, nature: str
) -> m.ModelElement:
    """Walk ``component.parent`` while the parent's ``nature`` matches.

    Returns the outermost component reached this way, or ``component``
    itself if its parent doesn't match (or isn't a ``PhysicalComponent``
    at all).
    """
    current = component
    seen_uuids = {component.uuid}
    while True:
        parent = current.parent
        if (
            type(parent).__name__ != "PhysicalComponent"
            or getattr(parent, "nature", None) != nature
            or parent.uuid in seen_uuids
        ):
            return current
        current = parent
        seen_uuids.add(current.uuid)


def resolve_outermost_behavior(component: m.ModelElement) -> m.ModelElement:
    """Walk up ``component``'s composition chain through Behavior layers.

    Returns the outermost ``nature == "BEHAVIOR"`` component reached by
    following ``.parent`` (e.g. an application that is a part of a
    middleware behavior, itself a part of an OS behavior), stopping
    before the first parent that isn't also a Behavior. Returns
    ``component`` itself if it has no Behavior-nature parent.
    """
    return _walk_parents_while(component, "BEHAVIOR")


def resolve_outermost_node(node: m.ModelElement) -> m.ModelElement:
    """Walk up ``node``'s physical composition chain through Node layers.

    Returns the outermost ``nature == "NODE"`` component reached by
    following ``.parent`` (e.g. a board inside a box inside a chassis),
    stopping before the first parent that isn't also a Node - notably,
    this does *not* walk as far as the root ``PhysicalComponent``
    (typically ``nature == "UNSET"``), which would otherwise make every
    node's "outermost" resolve to the same trivial system root. Returns
    ``node`` itself if it has no Node-nature parent.
    """
    return _walk_parents_while(node, "NODE")


def resolve_hosting_nodes(component: m.ModelElement) -> list[m.ModelElement]:
    """Find the Node(s) that ultimately run ``component``.

    ``component`` may be nested (via composition) inside other Behavior
    components - this first resolves to the outermost such Behavior
    (:func:`resolve_outermost_behavior`), then takes *its* direct
    ``deploying_components``, filtered to ``nature == "NODE"``. That's a
    single hop by design: deployment is reserved strictly for the
    Behavior-to-Node boundary, so if it doesn't land on a Node directly,
    that's treated as "not deployed anywhere" rather than something to
    keep searching through.
    """
    outermost = resolve_outermost_behavior(component)
    nodes: list[m.ModelElement] = []
    for parent in outermost.deploying_components:
        if getattr(parent, "nature", None) == "NODE" and parent not in nodes:
            nodes.append(parent)
    return nodes
