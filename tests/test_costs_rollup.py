"""Unit tests for costs/rollup.py — summing CostEntry-shaped rows into
whatever category/vendor/item breakdown is requested."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from costs.rollup import rollup


@dataclass
class FakeEntry:
    category: str
    vendor_name: str | None
    item_name: str
    amount: Decimal


def _rows():
    return [
        FakeEntry("Oranges", "Fresh Farms", "50kg Valencia oranges", Decimal("5000.00")),
        FakeEntry("Oranges", "Fresh Farms", "20kg Valencia oranges", Decimal("2000.00")),
        FakeEntry("Rent", "Landlord Co.", "Monthly rent", Decimal("30000.00")),
        FakeEntry("Staff Salaries", None, "August salary", Decimal("50000.00")),
    ]


def test_full_total_no_grouping():
    result = rollup(_rows(), group_by=())
    assert len(result) == 1
    assert result[0].total_amount == Decimal("87000.00")
    assert result[0].entry_count == 4


def test_group_by_category():
    result = rollup(_rows(), group_by=("category",))
    by_cat = {r.key["category"]: r for r in result}
    assert by_cat["Oranges"].total_amount == Decimal("7000.00")
    assert by_cat["Oranges"].entry_count == 2
    assert by_cat["Rent"].total_amount == Decimal("30000.00")


def test_group_by_vendor_none_stays_grouped_separately():
    result = rollup(_rows(), group_by=("vendor_name",))
    by_vendor = {r.key["vendor_name"]: r for r in result}
    assert by_vendor[None].total_amount == Decimal("50000.00")  # Staff Salaries has no vendor
    assert by_vendor["Fresh Farms"].total_amount == Decimal("7000.00")


def test_group_by_item_name_keeps_distinct():
    result = rollup(_rows(), group_by=("item_name",))
    assert len(result) == 4  # each row has a unique item_name here


def test_sorted_highest_total_first():
    result = rollup(_rows(), group_by=("category",))
    totals = [r.total_amount for r in result]
    assert totals == sorted(totals, reverse=True)


def test_unknown_dimension_raises():
    with pytest.raises(ValueError):
        rollup(_rows(), group_by=("not_a_real_dimension",))


def test_empty_rows_returns_empty_list():
    assert rollup([], group_by=("category",)) == []
