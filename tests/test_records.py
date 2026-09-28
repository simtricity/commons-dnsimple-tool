"""The pure record generators and report categories — no network, no DNSimple account."""

import pytest

from dnsimple_cli import app_setup, clerk_setup, email_setup, redirect_setup
from dnsimple_cli.report import PALETTE, get_categories


def triples(records):
    return [(r["name"], r["type"], r["content"]) for r in records]


def test_deno_www_with_apex_redirect():
    assert triples(app_setup.get_deno_records("example.com", "TOK")) == [
        ("www", "CNAME", "alias.deno.net"),
        ("_acme-challenge.www", "CNAME", "TOK._acme.deno.net."),
        ("", "URL", "https://www.example.com"),
    ]


def test_deno_apex_uses_alias():
    assert triples(app_setup.get_deno_apex_records("TOK")) == [
        ("", "ALIAS", "alias.deno.net"),
        ("_acme-challenge", "CNAME", "TOK._acme.deno.net."),
    ]


def test_deno_www_only():
    assert triples(app_setup.get_deno_www_only_records("TOK")) == [
        ("www", "CNAME", "alias.deno.net"),
        ("_acme-challenge.www", "CNAME", "TOK._acme.deno.net."),
    ]


def test_clerk_records_carry_the_instance():
    recs = triples(clerk_setup.get_clerk_records("example.com", "INST"))
    assert ("clerk", "CNAME", "frontend-api.clerk.services") in recs
    assert ("clkmail", "CNAME", "mail.INST.clerk.services") in recs
    assert len(recs) == 5


def test_redirect_covers_apex_and_wildcard():
    assert triples(redirect_setup.get_redirect_records("https://www.example.com")) == [
        ("", "URL", "https://www.example.com"),
        ("*", "URL", "https://www.example.com"),
    ]


def test_merge_spf_adds_once():
    base = "v=spf1 include:a.example ~all"
    assert email_setup.merge_spf(base, "b.example") == "v=spf1 include:a.example include:b.example ~all"
    assert email_setup.merge_spf(base, "a.example") == base


def test_improvmx_records():
    recs = email_setup.get_records_to_create("improvmx")
    assert [(r["type"], r["content"]) for r in recs][:2] == [
        ("MX", "mx1.improvmx.com"), ("MX", "mx2.improvmx.com")]


def test_categories_declared_in_the_recommendations_file():
    data = {"categories": [{"key": "a", "title": "Alphas", "label": "Alpha", "color": "blue"}],
            "groups": [{"category": "a"}]}
    (cat,) = get_categories(data)
    assert cat == {"key": "a", "title": "Alphas", "label": "Alpha", "color": "blue", "id": "a"}


def test_categories_fall_back_to_the_groups_in_order_seen():
    data = {"groups": [{"category": "zone_b"}, {"category": "zone_a"}, {"category": "zone_b"}]}
    cats = get_categories(data)
    assert [c["key"] for c in cats] == ["zone_b", "zone_a"]
    assert cats[0]["title"] == "Zone B" and cats[0]["id"] == "zone-b"
    assert all(c["color"] in PALETTE for c in cats)


@pytest.mark.parametrize("colour", ["", "neon"])
def test_unknown_colour_gets_a_palette_colour(colour):
    (cat,) = get_categories({"categories": [{"key": "a", "title": "A", "color": colour}]})
    assert cat["color"] in PALETTE
