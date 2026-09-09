# Copyright DB InfraGO AG and contributors
# SPDX-License-Identifier: Apache-2.0
"""Automatic verification of Capella ``Constraint`` objects against PVMT.

A :class:`~capellambse.metamodel.capellacore.Constraint` can be turned into
an auto-verifiable rule by:

1. Applying the ``Constraints.Constraints`` managed PVMT group to the
   constraint itself, with ``Expression`` (comparison operator),
   ``Numerical Value`` (threshold), ``Permission`` (severity/polarity) and
   optionally ``Unit`` (the unit the threshold is expressed in) filled in.
   ``Unit`` is the rule's own declared unit - it's independent of, and
   checked against, whatever unit the checked PVMT value itself carries
   (see the ``unit_ok`` flag on :class:`ConstraintCheckResult`).
2. Adding one or more :class:`~capellambse.metamodel.capellacore.AbstractPropertyValue`
   instances (i.e. actual PVMT parameters owned by *other* model elements)
   to the constraint's ``constrained_elements``.
3. Because Capella cannot visualize a link to a PVMT value directly, the
   owning model element of each checked property value should *also* be
   added to ``constrained_elements`` so the relationship stays visible and
   traceable. This module flags it (as a separate warning) when that
   backlink is missing, without hiding or affecting the pass/fail result.

The PVMT group/property names and the Expression/Permission literal
mappings are not hardcoded here - they're read from
``<TEMPLATES_DIR>/constraints.config.json`` (see :data:`_DEFAULT_CONFIG`
for the shape and built-in defaults), so a deployment can adapt them to
its own model's PVMT enum literals without rebuilding the image.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import operator
import pathlib
import typing as t

import capellambse.model as m
import markupsafe
from capellambse.metamodel import capellacore

from capella_model_explorer import constants as c

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "constraints.config.json"

_NONE_SUBJECT = markupsafe.Markup('<span style="color: red;">NONE</span>')

PROPERTY_VALUE_TYPES = (
    capellacore.StringPropertyValue,
    capellacore.IntegerPropertyValue,
    capellacore.BooleanPropertyValue,
    capellacore.FloatPropertyValue,
    capellacore.EnumerationPropertyValue,
)

_OPERATOR_FUNCS: dict[str, t.Callable[[t.Any, t.Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "ge": operator.ge,
    "le": operator.le,
    "gt": operator.gt,
    "lt": operator.lt,
}

_DEFAULT_CONFIG: dict[str, t.Any] = {
    "pvmt_group_name": "Constraints.Constraints",
    "property_names": {
        "expression": "Expression",
        "numerical_value": "Numerical Value",
        "permission": "Permission",
        "unit": "Unit",
    },
    "unit_property_name": "__UNIT__",
    "unit_literal_property_name": "__LITERAL__",
    "comparators": {
        "EQ": "eq",
        "NEQ": "ne",
        "GEQ": "ge",
        "LEQ": "le",
        "GE": "gt",
        "LE": "lt",
    },
    "permissions": {
        "SHALL": {"invert": False, "severity": "error"},
        "SHALL NOT": {"invert": True, "severity": "error"},
        "SHOULD": {"invert": False, "severity": "warning"},
        "SHOULD NOT": {"invert": True, "severity": "warning"},
    },
}


@functools.lru_cache(maxsize=1)
def _read_config_file(path: str, mtime: float) -> dict[str, t.Any]:
    del mtime  # part of the cache key only, to invalidate on edits
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_config() -> dict[str, t.Any]:
    """Load the PVMT auto-check config from the templates directory.

    Falls back to :data:`_DEFAULT_CONFIG` if ``constraints.config.json``
    doesn't exist (yet) or fails to parse. Cached by the file's mtime, so
    edits to the file (e.g. through a volume-mounted templates directory)
    take effect on the next request, without restarting the server.
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


@dataclasses.dataclass
class ConstraintRule:
    """A constraint's own verification rule, read from its PVMT."""

    expression: str
    threshold: float
    permission: str
    unit: str | None


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
    unit_ok: bool
    rule_unit: str | None
    target_unit: str | None
    message: str


def _enum_value(pv: t.Any) -> t.Any:
    """Return the plain value of a property value, resolving enums."""
    if isinstance(pv, capellacore.EnumerationPropertyValue):
        return pv.value.name if pv.value is not None else None
    return pv.value


def _unit_literal_value(pv: t.Any) -> t.Any:
    """Resolve an enumeration property value to its canonical unit literal.

    Both a numeric PVMT property's ``__UNIT__`` and a constraint rule's
    ``Unit`` are enumeration properties whose *chosen literal* itself
    carries a nested ``__LITERAL__`` property value holding the actual,
    comparable unit text (e.g. ``"km/h"``) - decoupled from the literal's
    own display name. Falls back to the plain enum name (:func:`_enum_value`)
    if that nested property isn't present, so a unit enum without this
    convention still resolves to something comparable.
    """
    if not isinstance(pv, capellacore.EnumerationPropertyValue):
        return _enum_value(pv)
    if pv.value is None:
        return None

    literal_property_name = _load_config()["unit_literal_property_name"]
    try:
        return pv.value.property_values.by_name(literal_property_name).value
    except (KeyError, AttributeError):
        return _enum_value(pv)


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

    unit_property_name = _load_config()["unit_property_name"]
    try:
        unit_pv = applied[0].property_values.by_name(unit_property_name)
    except KeyError:
        return None
    return _unit_literal_value(unit_pv)


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


def _rule_config(constraint: m.ModelElement) -> ConstraintRule | None:
    """Read this constraint's own verification rule from its PVMT.

    Returns a :class:`ConstraintRule`, or ``None`` if the constraint has
    no (complete) verification rule configured. ``Unit`` is optional -
    unlike ``Expression``/``Numerical Value``/``Permission``, a missing
    or unset ``Unit`` doesn't invalidate the rule, it just means
    :attr:`ConstraintRule.unit` is ``None`` (nothing to check against).
    """
    config = _load_config()
    try:
        group = constraint.applied_property_value_groups.by_name(
            config["pvmt_group_name"]
        )
    except KeyError:
        logger.debug(
            "Constraint %s has no %r PVMT group applied (applied groups: %s)",
            constraint.uuid,
            config["pvmt_group_name"],
            [g.name for g in constraint.applied_property_value_groups],
        )
        return None

    names = config["property_names"]
    required = {names["expression"], names["numerical_value"], names["permission"]}
    props = {pv.name: pv for pv in group.property_values}
    if not required <= props.keys():
        logger.debug(
            "Constraint %s's %r group is missing property value(s) %s"
            " (has: %s)",
            constraint.uuid,
            config["pvmt_group_name"],
            required - props.keys(),
            list(props.keys()),
        )
        return None

    expression = _enum_value(props[names["expression"]])
    threshold = props[names["numerical_value"]].value
    permission = _enum_value(props[names["permission"]])
    if expression is None or threshold is None or permission is None:
        logger.debug(
            "Constraint %s has an incomplete rule: expression=%r,"
            " threshold=%r, permission=%r",
            constraint.uuid,
            expression,
            threshold,
            permission,
        )
        return None

    unit_name = names.get("unit")
    unit = _unit_literal_value(props[unit_name]) if unit_name in props else None

    return ConstraintRule(expression, threshold, permission, unit)


def describe_constraint_rule(
    constraint: m.ModelElement,
) -> markupsafe.Markup | None:
    """Build a human-readable rule string from the constraint's PVMT config.

    Returns ``None`` only if the constraint has no verification rule
    configured at all (no PVMT group, or an incomplete one). If a rule
    *is* configured but none of ``constrained_elements`` are actual PVMT
    property values to check it against, the rule is still rendered -
    with its subject shown as a red "NONE" instead of a property name -
    so a misconfigured link is visible in the table rather than silently
    looking like a plain, unchecked constraint.
    """
    rule = _rule_config(constraint)
    if rule is None:
        return None

    threshold_str = (
        f"{rule.threshold} {rule.unit}" if rule.unit else f"{rule.threshold}"
    )

    values = [
        elt
        for elt in constraint.constrained_elements
        if isinstance(elt, PROPERTY_VALUE_TYPES)
    ]

    if not values:
        logger.debug(
            "Constraint %s has a rule but no PVMT property value among its"
            " constrained_elements (has: %s)",
            constraint.uuid,
            [type(elt).__name__ for elt in constraint.constrained_elements],
        )
        subject: str | markupsafe.Markup = _NONE_SUBJECT
    elif len(values) == 1:
        subject = values[0].name
    else:
        subject = "[" + ", ".join(v.name for v in values) + "]"

    return markupsafe.Markup("{} {} be {} {}").format(
        subject, rule.permission, rule.expression, threshold_str
    )


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

    config = _load_config()
    comparator = _OPERATOR_FUNCS.get(
        config["comparators"].get(rule.expression)
    )
    permission_info = config["permissions"].get(rule.permission)
    if comparator is None or permission_info is None:
        logger.warning(
            "Constraint %s has an unsupported Expression/Permission: %r/%r",
            constraint.uuid,
            rule.expression,
            rule.permission,
        )
        return []
    invert = permission_info["invert"]
    severity = permission_info["severity"]

    constrained = list(constraint.constrained_elements)
    constrained_uuids = {elt.uuid for elt in constrained}

    results: list[ConstraintCheckResult] = []
    for elt in constrained:
        if not isinstance(elt, PROPERTY_VALUE_TYPES):
            continue

        actual = _enum_value(elt)
        owner = getattr(getattr(elt, "parent", None), "parent", None)
        backlink_ok = owner is not None and owner.uuid in constrained_uuids

        target_unit = pvmt_unit(elt)
        unit_ok = (
            rule.unit is None
            or target_unit is None
            or rule.unit == target_unit
        )
        if not unit_ok:
            logger.debug(
                "Constraint %s's rule unit %r doesn't match %s's unit %r",
                constraint.uuid,
                rule.unit,
                elt.name,
                target_unit,
            )

        try:
            raw_passed = comparator(actual, rule.threshold)
        except TypeError:
            logger.warning(
                "Cannot compare %r %s %r for constraint %s",
                actual,
                rule.expression,
                rule.threshold,
                constraint.uuid,
            )
            raw_passed = False
        passed = (not raw_passed) if invert else raw_passed

        message = (
            f"{elt.name} = {actual!r} {rule.expression} {rule.threshold!r}"
            f" -> {'PASS' if passed else 'FAIL'} ({rule.permission})"
        )

        results.append(
            ConstraintCheckResult(
                constraint=constraint,
                property_value=elt,
                owner=owner,
                actual=actual,
                expression=rule.expression,
                threshold=rule.threshold,
                permission=rule.permission,
                passed=passed,
                severity=severity,
                backlink_ok=backlink_ok,
                unit_ok=unit_ok,
                rule_unit=rule.unit,
                target_unit=target_unit,
                message=message,
            )
        )

    return results
