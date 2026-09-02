# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0
"""Automatic verification of Capella ``Constraint`` objects against PVMT.

A :class:`~capellambse.metamodel.capellacore.Constraint` can be turned into
an auto-verifiable rule by:

1. Applying the ``Constraints.Constraints`` managed PVMT group to the
   constraint itself, with ``Expression`` (comparison operator),
   ``Numerical Value`` (threshold) and ``Permission`` (severity/polarity)
   filled in.
2. Adding one or more :class:`~capellambse.metamodel.capellacore.AbstractPropertyValue`
   instances (i.e. actual PVMT parameters owned by *other* model elements)
   to the constraint's ``constrained_elements``.
3. Because Capella cannot visualize a link to a PVMT value directly, the
   owning model element of each checked property value should *also* be
   added to ``constrained_elements`` so the relationship stays visible and
   traceable. This module flags it (as a separate warning) when that
   backlink is missing, without hiding or affecting the pass/fail result.
"""

from __future__ import annotations

import dataclasses
import logging
import operator
import typing as t

import capellambse.model as m
from capellambse.metamodel import capellacore

logger = logging.getLogger(__name__)

PVMT_GROUP_NAME = "Constraints.Constraints"

PROPERTY_VALUE_TYPES = (
    capellacore.StringPropertyValue,
    capellacore.IntegerPropertyValue,
    capellacore.BooleanPropertyValue,
    capellacore.FloatPropertyValue,
    capellacore.EnumerationPropertyValue,
)

_COMPARATORS: dict[str, t.Callable[[t.Any, t.Any], bool]] = {
    "EQ": operator.eq,
    "NEQ": operator.ne,
    "GEQ": operator.ge,
    "LEQ": operator.le,
    "GE": operator.gt,
    "LE": operator.lt,
}

# permission -> (invert result, severity)
_PERMISSIONS: dict[str, tuple[bool, str]] = {
    "SHALL": (False, "error"),
    "SHALL NOT": (True, "error"),
    "SHOULD": (False, "warning"),
    "SHOULD NOT": (True, "warning"),
}


@dataclasses.dataclass
class ConstraintCheckResult:
    """The outcome of verifying one PVMT-linked property value."""

    constraint: m.ModelElement
    property_value: m.ModelElement
    owner: m.ModelElement | None
    actual: t.Any
    expression: str
    threshold: float
    permission: str
    passed: bool
    severity: str
    backlink_ok: bool
    message: str


def _enum_value(pv: t.Any) -> t.Any:
    """Return the plain value of a property value, resolving enums."""
    if isinstance(pv, capellacore.EnumerationPropertyValue):
        return pv.value.name if pv.value is not None else None
    return pv.value


def pvmt_unit(pv: t.Any) -> str | None:
    """Return the ``__UNIT__`` value for an applied numeric PVMT property.

    Units are stored as a nested ``__UNIT__`` property value on the
    *definition* that a PVMT group's applied property links back to via
    ``applied_property_values``, not on the applied copy itself. This
    looks up that definition and returns its ``__UNIT__`` child's value,
    or ``None`` if the property isn't numeric or has no unit defined.
    """
    if not isinstance(
        pv, capellacore.IntegerPropertyValue | capellacore.FloatPropertyValue
    ):
        return None

    applied = pv.applied_property_values
    if not applied:
        return None

    try:
        return applied[0].property_values.by_name("__UNIT__").value
    except KeyError:
        return None


def find_related_constraints(
    model: m.MelodyModel,
    obj: m.ModelElement,
    *,
    below: m.ModelElement | None = None,
) -> list[m.ModelElement]:
    """Find every Constraint that actually relates to ``obj``.

    A constraint's ``ownedConstraints`` containment (i.e. ``obj.constraints``)
    only reflects whichever model element happened to "own" it at creation
    time in Capella's UI - not every element it's semantically relevant
    to. The authoritative link is ``constrained_elements``. This searches
    the whole model (or the subtree under ``below``, if given) and
    returns every Constraint where ``obj`` is either:

    - directly listed in ``constrained_elements``, or
    - the owner of a PVMT property value that is listed in
      ``constrained_elements`` (this matches even if the traceability
      backlink to the owner itself is missing - see
      :func:`evaluate_constraint`, which flags that separately).
    """
    related: list[m.ModelElement] = []
    for con in model.search("Constraint", below=below):
        for elt in con.constrained_elements:
            if elt == obj:
                related.append(con)
                break
            if isinstance(elt, PROPERTY_VALUE_TYPES):
                owner = getattr(getattr(elt, "parent", None), "parent", None)
                if owner == obj:
                    related.append(con)
                    break
    return related


def count_constraint_references(
    model: m.MelodyModel,
    *,
    below: m.ModelElement | None = None,
) -> dict[str, int]:
    """Count, per PVMT property value UUID, how many constraints reference it.

    Scans every Constraint's ``constrained_elements`` once (across the
    whole model, or the subtree under ``below``) and returns a mapping of
    property-value UUID to the number of constraints that list it.
    Property values with no entry in the returned dict are unconstrained.
    """
    counts: dict[str, int] = {}
    for con in model.search("Constraint", below=below):
        for elt in con.constrained_elements:
            if isinstance(elt, PROPERTY_VALUE_TYPES):
                counts[elt.uuid] = counts.get(elt.uuid, 0) + 1
    return counts


def _rule_config(
    constraint: m.ModelElement,
) -> tuple[str, float, str] | None:
    """Read this constraint's own verification rule from its PVMT.

    Returns a ``(expression, threshold, permission)`` tuple, or ``None``
    if the constraint has no (complete) verification rule configured.
    """
    try:
        group = constraint.applied_property_value_groups.by_name(
            PVMT_GROUP_NAME
        )
    except KeyError:
        return None

    props = {pv.name: pv for pv in group.property_values}
    if not {"Expression", "Numerical Value", "Permission"} <= props.keys():
        return None

    expression = _enum_value(props["Expression"])
    threshold = props["Numerical Value"].value
    permission = _enum_value(props["Permission"])
    if expression is None or threshold is None or permission is None:
        return None

    return expression, threshold, permission


def evaluate_constraint(
    constraint: m.ModelElement,
) -> list[ConstraintCheckResult]:
    """Evaluate a Constraint against every PVMT value it constrains.

    For each item in ``constraint.constrained_elements`` that is itself a
    PVMT property value, the constraint's own ``Expression``/``Numerical
    Value``/``Permission`` rule (read from its ``Constraints.Constraints``
    PVMT group) is applied to that value's actual content.

    Returns an empty list if the constraint has no verification rule
    configured, or if none of its constrained elements are property
    values (i.e. it's a plain, manually-reviewed constraint).
    """
    rule = _rule_config(constraint)
    if rule is None:
        return []
    expression, threshold, permission = rule

    comparator = _COMPARATORS.get(expression)
    permission_info = _PERMISSIONS.get(permission)
    if comparator is None or permission_info is None:
        logger.warning(
            "Constraint %s has an unsupported Expression/Permission: %r/%r",
            constraint.uuid,
            expression,
            permission,
        )
        return []
    invert, severity = permission_info

    constrained = list(constraint.constrained_elements)
    constrained_uuids = {elt.uuid for elt in constrained}

    results: list[ConstraintCheckResult] = []
    for elt in constrained:
        if not isinstance(elt, PROPERTY_VALUE_TYPES):
            continue

        actual = _enum_value(elt)
        owner = getattr(getattr(elt, "parent", None), "parent", None)
        backlink_ok = owner is not None and owner.uuid in constrained_uuids

        try:
            raw_passed = comparator(actual, threshold)
        except TypeError:
            logger.warning(
                "Cannot compare %r %s %r for constraint %s",
                actual,
                expression,
                threshold,
                constraint.uuid,
            )
            raw_passed = False
        passed = (not raw_passed) if invert else raw_passed

        message = (
            f"{elt.name} = {actual!r} {expression} {threshold!r}"
            f" -> {'PASS' if passed else 'FAIL'} ({permission})"
        )

        results.append(
            ConstraintCheckResult(
                constraint=constraint,
                property_value=elt,
                owner=owner,
                actual=actual,
                expression=expression,
                threshold=threshold,
                permission=permission,
                passed=passed,
                severity=severity,
                backlink_ok=backlink_ok,
                message=message,
            )
        )

    return results
