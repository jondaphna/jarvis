"""The grant parser decides whether JARVIS may spend your money. Test it hard."""

from __future__ import annotations

import pytest

from jarvis.core.grants import (
    CAP_COMMS_SEND, CAP_PAYMENT_SPEND, CAP_WEB_PUBLISH, parse_app_allowances,
    parse_grants,
)


class TestNoFalsePositives:
    """A false positive here means JARVIS spends money it was never allowed to."""

    @pytest.mark.parametrize("text", [
        "Write the scripts but don't post anything.",
        "You can see why that failed, right?",
        "Can you buy milk on the way home?",
        "I bought a subscription yesterday.",
        "Explain how posting to TikTok works.",
        "Never post anything without asking me.",
        "please post this to youtube",
        "Research competitors and summarise. Please email me the result.",
        "What would it cost to buy Kling credits?",
        "",
        "   ",
    ])
    def test_no_grant_produced(self, text):
        assert parse_grants(text) == []


class TestExplicitAuthorisation:
    def test_purchase_with_limit_and_service(self):
        grants = parse_grants(
            "You have permission to buy Kling credits this time, up to $20.")
        assert len(grants) == 1
        grant = grants[0]
        assert CAP_PAYMENT_SPEND in grant.capabilities
        assert grant.max_amount_usd == 20.0
        assert grant.uses == 1
        assert any("kling" in r for r in grant.resources)

    def test_publishing_bound_to_named_platform(self):
        grants = parse_grants(
            "Make the videos and post them to TikTok - you're allowed to upload "
            "them, today only.")
        assert len(grants) == 1
        assert CAP_WEB_PUBLISH in grants[0].capabilities
        assert any("tiktok" in r for r in grants[0].resources)
        assert grants[0].expires_at is not None

    def test_two_authorisations_stay_separate(self):
        """The bug that mattered: one sentence's scope leaking into another's."""
        grants = parse_grants(
            "Post them to TikTok - you have permission to upload them tonight. "
            "You may also buy Kling credits this time, up to $20.")
        by_capability = {g.capabilities[0]: g for g in grants}
        assert CAP_WEB_PUBLISH in by_capability
        assert CAP_PAYMENT_SPEND in by_capability
        # The upload grant must NOT inherit the purchase's $20 cap or Kling scope.
        upload = by_capability[CAP_WEB_PUBLISH]
        assert upload.max_amount_usd is None
        assert any("tiktok" in r for r in upload.resources)
        assert not any("kling" in r for r in upload.resources)

    def test_soft_lead_needs_a_scope_marker(self):
        assert parse_grants("You may email the summary, just once.")
        assert parse_grants("please email the client") == []

    def test_unqualified_grant_is_single_use(self):
        grant = parse_grants("You have permission to post this.")[0]
        assert grant.uses == 1

    def test_standing_permission(self):
        grant = parse_grants("From now on you may post to Instagram whenever you want.")[0]
        assert grant.uses is None


class TestGrantMatching:
    def test_spend_limit_is_cumulative(self):
        grant = parse_grants(
            "You have permission to spend up to $20 on Kling, 5 times.")[0]
        assert grant.covers(CAP_PAYMENT_SPEND, "kling", 15.0)
        grant.consume(15.0)
        # 15 spent of 20; a second $15 purchase must not fit.
        assert not grant.covers(CAP_PAYMENT_SPEND, "kling", 15.0)
        assert grant.covers(CAP_PAYMENT_SPEND, "kling", 4.0)

    def test_service_scope_is_enforced(self):
        grant = parse_grants("You have permission to buy Kling credits, once.")[0]
        assert grant.covers(CAP_PAYMENT_SPEND, "kling")
        assert not grant.covers(CAP_PAYMENT_SPEND, "heygen")

    def test_capability_scope_is_enforced(self):
        grant = parse_grants("You have permission to post to TikTok, once.")[0]
        assert not grant.covers(CAP_PAYMENT_SPEND, "tiktok")

    def test_a_spend_with_no_stated_price_does_not_fit_a_capped_grant(self):
        """A cap can only be checked against a number.

        The check used to run only when an amount was supplied, so a request
        that named no price matched a grant with a price limit - which is the
        one request the limit exists to stop.
        """
        grant = parse_grants(
            "You have permission to spend up to $20 on Kling, 5 times.")[0]
        assert not grant.covers(CAP_PAYMENT_SPEND, "kling")
        assert not grant.covers(CAP_PAYMENT_SPEND, "kling", None)

    @pytest.mark.parametrize("amount", [
        -100.0, float("inf"), float("-inf"), float("nan"), True, "5",
    ])
    def test_a_nonsense_price_never_fits_a_capped_grant(self, amount):
        grant = parse_grants(
            "You have permission to spend up to $20 on Kling, 5 times.")[0]
        assert not grant.covers(CAP_PAYMENT_SPEND, "kling", amount)

    def test_a_negative_amount_cannot_credit_the_budget(self):
        """`consume` added whatever it was given, so a negative *refunded*."""
        grant = parse_grants(
            "You have permission to spend up to $20 on Kling, 5 times.")[0]
        grant.consume(15.0)
        grant.consume(-100.0)
        assert grant.spent_usd == 15.0
        assert not grant.covers(CAP_PAYMENT_SPEND, "kling", 15.0)

    def test_uses_are_exhausted(self):
        grant = parse_grants("You have permission to email them, just once.")[0]
        assert grant.covers(CAP_COMMS_SEND)
        grant.consume()
        assert not grant.covers(CAP_COMMS_SEND)
        assert grant.expired


class TestAppAllowances:
    @pytest.mark.parametrize("text,expected", [
        ("JARVIS, you can use Photoshop for the thumbnails.", ["Photoshop"]),
        ("You may use CapCut and Premiere Pro from now on.", ["CapCut", "Premiere Pro"]),
        ("you are allowed to use Excel, Word and Outlook", ["Excel", "Word", "Outlook"]),
        ("Add Spotify to your apps", ["Spotify"]),
    ])
    def test_extracts_app_names(self, text, expected):
        assert parse_app_allowances(text) == expected

    @pytest.mark.parametrize("text", [
        "you can use it whenever",
        "You can see the file",
        "allow me to explain",
    ])
    def test_ignores_non_apps(self, text):
        assert parse_app_allowances(text) == []
