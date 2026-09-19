# SPDX-License-Identifier: Apache-2.0
"""Complete hashed fixture pages retain source data and independent expectations."""

from copy import deepcopy
from dataclasses import replace

import pytest
from test_flow_native_verification import scenario

from sanka_extensions.flow import (
    ArtifactIdentity,
    NativeBillingFixtureManifest,
    NativeBillingFixturePage,
    NativeBillingFixturePageRef,
    NativeBillingFixtureRecord,
)
from sanka_extensions.flow.native_fixtures import decimal_value


def fields(*, invoice=False):
    result = {
        "currency": "JPY",
        "total_price": "220",
        "total_price_without_tax": "200",
        "tax": "10",
        "tax_inclusive": False,
        "line_items": [
            {
                "key": "line-a",
                "name": "Test product",
                "quantity": "2",
                "unit_price": "100",
                "tax_rate": "10",
            }
        ],
    }
    if invoice:
        result.update({"status": "draft", "invoice_date": "2026-09-14", "due_date": "2026-10-14"})
    return result


def record(key, *, existing=False, existing_order=False):
    order, invoice = fields(), fields(invoice=True)
    return NativeBillingFixtureRecord(
        key,
        "customer-a",
        {"id": key, "amount": "220", "currency": "JPY"},
        order,
        invoice,
        initial_order=order if existing or existing_order else None,
        initial_invoice={**invoice, "status": "sent", "notes": "Preserve this user edit"}
        if existing
        else None,
    )


def fixture():
    selected = scenario()
    page = NativeBillingFixturePage(
        tuple(
            record(key, existing=key == "deal-a", existing_order=key == "deal-b")
            for key in selected.record_keys
        )
    )
    page_ref = NativeBillingFixturePageRef(
        ArtifactIdentity("page-1", "1", page.digest), selected.record_keys
    )
    manifest = NativeBillingFixtureManifest(
        selected.mapping,
        selected.configuration_digest,
        ("deal-a", "deal-b"),
        ("deal-a",),
        (page_ref,),
    )
    return (
        replace(selected, fixture=ArtifactIdentity("fixture", "1", manifest.digest)),
        manifest,
        page,
    )


def test_manifest_pages_and_independent_readback_round_trip():
    selected, manifest, page = fixture()
    manifest.validate_for(selected)
    manifest.validate_page(0, page)
    assert NativeBillingFixtureManifest.from_dict(manifest.to_dict()).digest == manifest.digest
    assert NativeBillingFixturePage.from_dict(page.to_dict()).digest == page.digest
    assert page.records[0].to_dict()["initial_invoice"]["status"] == "sent"
    assert page.records[0].to_dict()["invoice"]["status"] == "draft"


def test_record_input_and_returned_values_cannot_change_fixture():
    source = {"id": "source-a"}
    original = fields()
    candidate = NativeBillingFixtureRecord(
        "a", "customer-a", source, original, fields(invoice=True)
    )
    source["id"] = "changed"
    original["currency"] = "USD"
    candidate.to_dict()["invoice"]["status"] = "sent"
    assert candidate.to_dict()["source"] == {"id": "source-a"}
    assert candidate.to_dict()["order"]["currency"] == "JPY"
    assert candidate.to_dict()["invoice"]["status"] == "draft"


def test_tampered_page_is_rejected_even_if_membership_is_unchanged():
    _, manifest, page = fixture()
    data = page.to_dict()
    data["records"][0]["source"]["amount"] = "999"
    with pytest.raises(ValueError, match="pinned artifact"):
        manifest.validate_page(0, NativeBillingFixturePage.from_dict(data))


def test_missing_or_duplicate_page_membership_is_rejected():
    _, manifest, page = fixture()
    wrong_ref = replace(manifest.pages[0], record_keys=("deal-a", "deal-b"))
    with pytest.raises(ValueError, match="complete membership"):
        wrong_ref.validate_page(page)
    with pytest.raises(ValueError, match="disjoint"):
        replace(manifest, pages=(*manifest.pages, manifest.pages[0]))


def test_manifest_cannot_claim_existing_invoice_without_seed_snapshot():
    _, manifest, _ = fixture()
    page = NativeBillingFixturePage(tuple(record(key) for key in ("deal-a", "deal-b", "deal-c")))
    reference = replace(manifest.pages[0], artifact=ArtifactIdentity("page-1", "1", page.digest))
    manifest = replace(manifest, pages=(reference,))
    with pytest.raises(ValueError, match="baseline"):
        manifest.validate_page(0, page)


@pytest.mark.parametrize(
    "value", [True, 1, 1.0, "1.0", "NaN", "Infinity", "-0", "1e100000", "01", "1e2"]
)
def test_decimal_expectations_have_no_float_tolerance_or_unbounded_exponent(value):
    with pytest.raises(ValueError):
        decimal_value(value, "price")


@pytest.mark.parametrize(
    "field", ["currency", "tax", "line_items", "total_price", "total_price_without_tax"]
)
def test_oracle_cannot_omit_required_business_fields(field):
    order = fields()
    del order[field]
    with pytest.raises(ValueError, match="must assert"):
        NativeBillingFixtureRecord("a", "customer-a", {"id": "a"}, order, fields(invoice=True))


def test_new_invoice_cannot_assert_sent_status_or_unbounded_page():
    with pytest.raises(ValueError, match="remain draft"):
        NativeBillingFixtureRecord(
            "a", "customer-a", {"id": "a"}, fields(), {**fields(invoice=True), "status": "sent"}
        )
    with pytest.raises(ValueError, match="one to 100"):
        NativeBillingFixturePage(tuple(record(f"a-{i}") for i in range(101)))


def test_existing_invoice_oracle_must_distinguish_user_changes_from_regeneration():
    with pytest.raises(ValueError, match="preserved user change"):
        NativeBillingFixtureRecord(
            "a",
            "customer-a",
            {"id": "a"},
            fields(),
            fields(invoice=True),
            initial_order=fields(),
            initial_invoice=fields(invoice=True),
        )


def test_fixture_rejects_unknown_executable_metadata():
    selected, manifest, page = fixture()
    for original, decode in (
        (manifest.to_dict(), NativeBillingFixtureManifest.from_dict),
        (page.to_dict(), NativeBillingFixturePage.from_dict),
        (selected.to_dict(), type(selected).from_dict),
    ):
        payload = deepcopy(original)
        payload["before_run_script"] = "execute this"
        with pytest.raises(ValueError, match="unexpected"):
            decode(payload)
