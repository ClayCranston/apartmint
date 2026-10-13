import pytest

from apartmint import pipeline
from apartmint.config import Config, SafeError, Source
from apartmint.market import confirmed_price, market_report, recent_events


def extraction(evidence, *, unit="A101", availability="available", rent=1800):
    return {"listings": [{"unit": unit, "availability": availability, "rent": rent,
                          "bedrooms": None, "evidence": evidence}]}


class Client:
    def __init__(self, value):
        self.value = value

    def extract(self, text):
        return self.value


@pytest.mark.parametrize("evidence", [
    "Unit A101 information. Unit A102 available. Monthly rent USD 1800.",
    "Unit A101 information. Apartment A102 available. Monthly rent USD 1800.",
    "Unit A101 information. Apt. #A102 available. Monthly rent USD 1800.",
    "Unit A101 information. Suite A102 available. Monthly rent USD 1800.",
    "Unit A101 information. #A102 available. Monthly rent USD 1800.",
])
def test_explicit_other_unit_cannot_supply_availability_or_rent(evidence):
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(evidence), evidence)


def test_unit_substring_is_not_a_unit_identifier():
    text = "Unit A101 available. Monthly rent USD 1800."
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text, unit="101"), text)


@pytest.mark.parametrize("reference", [
    "this unit is advertised", "the unit has a balcony", "that apartment was renovated",
    "This suite may have parking", "each unit can have a separate deposit",
])
def test_anaphoric_unit_reference_does_not_invent_another_identifier(reference):
    text = f"Unit A101 available. Monthly rent USD 1800. Additional information: {reference}."
    assert pipeline.validate_extraction(extraction(text), text)[0]["identity"] == "a101"


@pytest.mark.parametrize("price_description", [
    "Monthly rent starts at USD 1700", "Monthly rent range USD 1700–1900",
])
def test_anaphoric_unit_reference_keeps_listing_with_unknown_exact_price(price_description):
    text = f"Unit A101 available. 1 bedroom. {price_description}. The exact monthly price for this unit is not published."
    value = extraction(text, rent=None)
    value["listings"][0]["bedrooms"] = 1
    row = pipeline.validate_extraction(value, text)[0]
    assert (row["identity"], row["availability"], row["rent"], row["bedrooms"]) == ("a101", "available", None, 1)


def test_anaphoric_reference_does_not_turn_tour_availability_into_vacancy():
    text = "Unit A101 has tours available by appointment. Monthly rent USD 1800. Current vacancy for this unit is unpublished."
    assert pipeline.validate_extraction(extraction(text, availability="unknown"), text)[0]["availability"] == "unknown"
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text), text)


@pytest.mark.parametrize("other_unit", ["the unit A102", "this unit A102", "that apartment Elm"])
def test_determiner_before_explicit_other_unit_does_not_bypass_guard(other_unit):
    text = f"Unit A101 information. Separate listing: {other_unit} available. Monthly rent USD 1800."
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text), text)


@pytest.mark.parametrize("unit, description", [
    ("A101", "A101"), ("A101", "Unit A101"), ("A101", "Unit#A101"),
    ("A101", "Apartment A101"), ("A101", "Apt. #A101"), ("A101", "Suite A101"),
    ("Unit A101", "Unit Unit A101"), ("#A101", "Unit #A101"),
])
def test_supported_unit_phrasing_still_accepts_its_own_facts(unit, description):
    text = description + " available. Monthly rent USD 1800."
    assert pipeline.validate_extraction(extraction(text, unit=unit), text)[0]["identity"] == "a101"


@pytest.mark.parametrize("unit", ["A1.01", "101/A", "A-101"])
def test_punctuation_inside_a_unit_identifier_is_preserved(unit):
    text = f"Unit {unit} available. Monthly rent USD 1800."
    assert pipeline.validate_extraction(extraction(text, unit=unit), text)[0]["identity"] == unit.casefold()


@pytest.mark.parametrize("text", [
    "Unit A101 has tours available by appointment.",
    "Tours of Unit A101 are available by appointment.",
    "Unit A101 available for a viewing.",
    "Unit A101 has viewing appointments available.",
    "Open houses are available for Unit A101.",
])
def test_tour_availability_is_not_unit_availability(text):
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text, rent=None), text)
    assert pipeline.validate_extraction(extraction(text, availability="unknown", rent=None), text)[0]["availability"] == "unknown"


def test_tour_unavailability_does_not_claim_unit_unavailability():
    text = "Unit A101 has no tours available this week."
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text, availability="unavailable", rent=None), text)


@pytest.mark.parametrize("status, text", [
    ("available", "Unit A101 available. Tours unavailable this week. Monthly rent USD 1800."),
    ("unavailable", "Unit A101 leased. Viewing appointments available next week. Monthly rent USD 1800."),
])
def test_separate_explicit_unit_status_survives_tour_information(status, text):
    assert pipeline.validate_extraction(extraction(text, availability=status), text)[0]["availability"] == status


@pytest.mark.parametrize("style", [
    "display:none", "DISPLAY: none !important", "display: /* obsolete */ none;",
    "visibility:hidden", "visibility: collapse", "color: red; display: none;",
    "display:none!important;display:block",
])
def test_plainly_hidden_inline_prices_do_not_reach_extraction(style):
    html = f'<div style="{style}"><span>Unit A101 available. Monthly rent USD 900.</span></div>'
    html += "<p>Unit A101 available. Monthly rent USD 1800.</p>"
    text = pipeline.sanitize_html(html)
    assert text == "Unit A101 available. Monthly rent USD 1800."
    assert pipeline.validate_extraction(extraction(text), text)[0]["rent"] == 1800


def test_visible_inline_override_is_not_mistaken_for_hidden_content():
    assert pipeline.sanitize_html('<p style="display:none; display:block">Visible unit</p>') == "Visible unit"


@pytest.mark.parametrize("tag", ["del", "s", "strike"])
def test_obsolete_crossed_out_rent_does_not_override_current_price(tag):
    html = f"<p>Unit A101 available. Monthly rent<{tag}>USD 2000</{tag}>USD 1850.</p>"
    text = pipeline.sanitize_html(html)
    assert text == "Unit A101 available. Monthly rent USD 1850."
    assert pipeline.validate_extraction(extraction(text, rent=1850), text)[0]["rent"] == 1850
    assert confirmed_price(text, 1850, "rental", "USD")
    with pytest.raises(SafeError, match="evidence"):
        pipeline.validate_extraction(extraction(text, rent=2000), text)


def test_cross_unit_quote_cannot_change_stored_price_or_create_drop(tmp_path):
    config = Config("Synthetic area", (Source("building", "https://example.com/availability"),))
    source = config.sources[0]
    original = "Unit A101 available. Monthly rent USD 2000."
    pipeline.save_snapshot(config, source, tmp_path, original)
    pipeline.analyze(config, source.id, tmp_path, Client(extraction(original, rent=2000)))

    borrowed = "Unit A101 information. Unit A102 available. Monthly rent USD 1800."
    pipeline.save_snapshot(config, source, tmp_path, borrowed)
    with pytest.raises(SafeError, match="evidence"):
        pipeline.analyze(config, source.id, tmp_path, Client(extraction(borrowed)))
    connection = pipeline.database(tmp_path)
    try:
        assert connection.execute("SELECT unit,rent FROM listings").fetchall() == [("A101", 2000)]
    finally:
        connection.close()
    report = market_report(tmp_path)["areas"][0]
    assert report["price_drops"] == 0 and report["observations"] == 1

    current = "Unit A101 available. Monthly rent USD 1850."
    pipeline.save_snapshot(config, source, tmp_path, current)
    pipeline.analyze(config, source.id, tmp_path, Client(extraction(current, rent=1850)))
    drops = [event for event in recent_events(tmp_path) if event["type"] == "price_drop"]
    assert [(event["old_price"], event["new_price"]) for event in drops] == [("2000.00", "1850.00")]


def test_collector_snapshot_omits_hidden_and_obsolete_price_data(tmp_path, monkeypatch):
    config = Config("Synthetic area", (Source("building", "https://example.com/availability"),))
    html = '<div style="display:none">Unit A101 available. Monthly rent USD 900.</div>'
    html += '<p>Unit A101 available. Monthly rent <del>USD 2000</del> USD 1850.</p>'
    monkeypatch.setattr(pipeline, "fetch_public_html", lambda *args: html)
    pipeline.collect(config, "building", tmp_path)
    document = pipeline.load_snapshot(config, config.sources[0], tmp_path)
    assert document["text"] == "Unit A101 available. Monthly rent USD 1850."
    pipeline.analyze(config, "building", tmp_path, Client(extraction(document["text"], rent=1850)))
    event = recent_events(tmp_path)[0]
    assert event["type"] == "first_seen" and event["new_price"] == "1850.00"
