# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0

"""Project adjustments to the capellambse modelling rules.

capellambse ships virtual types that exclude a layer's *root* function
from the function rules, on the grounds that the root is a container
rather than real behaviour. The same reasoning applies one level down:
any function that owns sub-functions is there to organise them, and it
is the children that carry the allocation and the exchanges. capellambse
stops at the root, so an intermediate parent function still gets asked
to be allocated and to own ports.

This module narrows the two affected rules to leaf functions and adds
the opposite check for parent functions. It also widens the "has a
description" rule, which capellambse applies to the boxes of a model but
not to the connections between them, and asks an interface to carry
something - capellambse checks that a functional exchange has an
interface, never that an interface has any exchanges.
"""

from __future__ import annotations

import dataclasses
import logging
import typing as t

import capellambse
import capellambse.model as m
from capellambse.extensions import validation
from capellambse.metamodel import la, oa, pa, sa

from capella_model_explorer import core

logger = logging.getLogger(__name__)

LEAF_RULE_SCOPES: dict[str, tuple[str, ...]] = {
    "Rule-011": ("LeafSystemFunction", "LeafOperationalActivity"),
    "SF-040": ("LeafSystemFunction",),
}
"""Rules that only make sense for a function that does the work itself.

Maps a capellambse rule ID to the virtual types it should apply to,
replacing the ones the rule was registered with.
"""

DESCRIBED_CLASSES: tuple[str, ...] = (
    "ComponentExchange",
    "FunctionalExchange",
)
"""Classes to add to the "has a description" rule.

capellambse applies Rule-001 to functions, components, actors, entities,
capabilities and states, but not to the things that connect them. An
interface carries a page of its own here, and that page now leads with
its description, so a missing one is worth flagging rather than leaving
the compliance table with nothing to say.
"""

_initialized = False


def _is_root(obj: m.ModelElement, layer: str) -> bool:
    root = getattr(getattr(obj._model, layer, None), "root_function", None)
    return root is not None and obj == root


def _register_virtual_types() -> None:
    """Register leaf/parent variants of the four function types.

    The leaf variants exclude the layer's root function, mirroring what
    capellambse's own virtual types of the same real type do, so that
    the rules narrowed to them keep behaving exactly as before for every
    function they still apply to.

    The parent variants deliberately include the root. It is the most
    parent-like function there is, and the rule asked of parents - own
    no ports - is one a root satisfies trivially, so including it costs
    a passing row rather than a false alarm. Leaving it out would show
    an empty compliance table on every root's report page.
    """

    def define(cls: type[m.ModelElement], layer: str, name: str, leaf: bool):
        def matches(obj: m.ModelElement) -> bool:
            if leaf and _is_root(obj, layer):
                return False
            return core.is_leaf_function(obj) is leaf

        matches.__name__ = name
        validation.virtual_type(cls)(matches)

    for cls, layer, label in (
        (oa.OperationalActivity, "oa", "OperationalActivity"),
        (sa.SystemFunction, "sa", "SystemFunction"),
        (la.LogicalFunction, "la", "LogicalFunction"),
        (pa.PhysicalFunction, "pa", "PhysicalFunction"),
    ):
        define(cls, layer, f"Leaf{label}", leaf=True)
        define(cls, layer, f"Parent{label}", leaf=False)


def _narrow_leaf_rules(rules: validation.Rules) -> None:
    """Restrict rules that assume a function does its own work."""
    for rule_id, types in LEAF_RULE_SCOPES.items():
        rule = rules.get(rule_id)
        if rule is None:
            logger.warning(
                "Cannot narrow unknown modelling rule %r -"
                " it may have been renamed upstream",
                rule_id,
            )
            continue
        rules[rule_id] = dataclasses.replace(rule, types=frozenset(types))


def _widen_description_rule(rules: validation.Rules) -> None:
    """Ask connections for a description too, not just the boxes."""
    rule = rules.get("Rule-001")
    if rule is None:
        logger.warning(
            "Cannot widen unknown modelling rule 'Rule-001' -"
            " it may have been renamed upstream"
        )
        return
    rules["Rule-001"] = dataclasses.replace(
        rule, types=rule.types | frozenset(DESCRIBED_CLASSES)
    )


def _register_parent_rules() -> None:
    @validation.rule(
        id="CME-PF-001",
        category=validation.Category.REQUIRED,
        types=[
            "ParentOperationalActivity",
            "ParentSystemFunction",
            "ParentLogicalFunction",
            "ParentPhysicalFunction",
        ],
        name="A parent function shall have no inputs or outputs",
        rationale=(
            "A function that owns sub-functions exists to organise them."
            " The work, and with it every exchange, belongs to its"
            " children. A port on the parent duplicates one of theirs and"
            " leaves it ambiguous which of the two an exchange really"
            " connects to."
        ),
        action=(
            "Move the exchange to the sub-function that produces or"
            " consumes it, and delete the port on the parent."
        ),
    )
    def parent_function_has_no_ports(obj: m.ModelElement) -> bool:
        return not obj.inputs and not obj.outputs


def _register_interface_rules() -> None:
    @validation.rule(
        id="CME-IF-001",
        category=validation.Category.REQUIRED,
        types=["ComponentExchange"],
        name="An interface shall carry at least one functional exchange",
        rationale=(
            "An interface exists so that functions on either side can"
            " exchange something. One with nothing allocated to it is a"
            " line on a diagram that carries no information: it asserts"
            " that two components are connected without saying what"
            " passes between them, and nothing downstream - an interface"
            " specification, a test, a budget - can be derived from it."
        ),
        action=(
            "Allocate the functional exchanges that this interface"
            " realises, or delete the interface if nothing crosses it."
        ),
    )
    def interface_carries_a_functional_exchange(obj: m.ModelElement) -> bool:
        return bool(obj.allocated_functional_exchanges)


def init(model: capellambse.MelodyModel) -> None:
    """Apply the project's modelling rule adjustments.

    The capellambse rule registry is global, so this runs once per
    process no matter how often the model is reloaded.
    """
    global _initialized
    if _initialized:
        return

    rules = t.cast("validation.Rules", model.validation.rules)
    _register_virtual_types()
    _narrow_leaf_rules(rules)
    _widen_description_rule(rules)
    _register_parent_rules()
    _register_interface_rules()
    _initialized = True
    logger.info(
        "Narrowed %d modelling rules to leaf functions, widened Rule-001 to"
        " %d more classes and added CME-PF-001, CME-IF-001",
        len(LEAF_RULE_SCOPES),
        len(DESCRIBED_CLASSES),
    )
