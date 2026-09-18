"""The schedule parser, which is the part a wrong answer hides in.

A scheduler that fires at the wrong time is not obviously broken. It looks
exactly like a scheduler that works, until the morning briefing arrives at
half past eight, or on Friday the thirteenth and no other day, or never. So
this file is mostly arithmetic: given this line and this moment, when next.

The cases worth having are the ones where a reasonable implementation is
quietly wrong: Sunday written as 7, a day-of-month and a day-of-week in the
same line, a step that does not divide evenly, the last minute of the year,
and February the thirtieth.
"""

from datetime import datetime, timezone

import pytest

from routines.cron import HORIZON_DAYS, ScheduleError, normalise, parse, valid


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestWritingItDown:
    """The friendly forms, which is what anyone will actually type."""

    @pytest.mark.parametrize("spoken,cron", [
        ("07:30", "30 7 * * *"),
        ("7:30", "30 7 * * *"),
        ("at 07:30", "30 7 * * *"),
        ("23:59", "59 23 * * *"),
        ("00:00", "0 0 * * *"),
        ("7.30", "30 7 * * *"),
        ("9:15 pm", "15 21 * * *"),
        ("12:00 am", "0 0 * * *"),
        ("12:30 pm", "30 12 * * *"),
        ("@daily", "0 0 * * *"),
        ("@hourly", "0 * * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
        ("every 15 minutes", "*/15 * * * *"),
        ("every minute", "* * * * *"),
        ("every 2 hours", "0 */2 * * *"),
        ("every hour", "0 * * * *"),
        ("every day", "0 0 * * *"),
        ("weekdays at 07:30", "30 7 * * 1-5"),
        ("weekends at 10:00", "0 10 * * 0,6"),
        ("30 7 * * *", "30 7 * * *"),
        ("  30   7  *  *  * ", "30 7 * * *"),
    ])
    def test_a_person_can_write_it_the_way_they_say_it(self, spoken, cron):
        assert normalise(spoken) == cron

    @pytest.mark.parametrize("spoken", [
        "", "   ", "half past seven", "30 7 * *", "30 7 * * * *",
        "25:00", "07:99", "13:00 pm", "every 0 minutes",
        "every 90 minutes", "every 30 hours", "every 3 days",
    ])
    def test_what_it_refuses(self, spoken):
        with pytest.raises(ScheduleError):
            normalise(spoken)

    def test_the_refusal_says_something_useful(self):
        with pytest.raises(ScheduleError) as caught:
            parse("30 7 * *")
        assert "five fields" in str(caught.value)

    def test_valid_is_the_question_a_settings_panel_asks(self):
        assert valid("07:30")
        assert not valid("nonsense o'clock")


class TestFields:
    """Ranges, steps, lists and names inside one field."""

    def test_a_star_is_everything(self):
        assert parse("* * * * *").minutes == frozenset(range(60))

    def test_a_range(self):
        assert parse("0 9-17 * * *").hours == frozenset(range(9, 18))

    def test_a_step_over_a_range(self):
        assert parse("0 9-17/4 * * *").hours == frozenset({9, 13, 17})

    def test_a_step_over_everything(self):
        assert parse("*/20 * * * *").minutes == frozenset({0, 20, 40})

    def test_a_list(self):
        assert parse("0 6,12,18 * * *").hours == frozenset({6, 12, 18})

    def test_a_list_of_ranges(self):
        assert parse("0 1-3,20-21 * * *").hours == frozenset({1, 2, 3, 20, 21})

    def test_month_names(self):
        assert parse("0 0 1 jan,jul *").months == frozenset({1, 7})

    def test_day_names(self):
        assert parse("0 9 * * mon-fri").weekdays == frozenset({1, 2, 3, 4, 5})

    def test_long_day_names_work_too(self):
        assert parse("0 9 * * monday").weekdays == frozenset({1})

    def test_sunday_may_be_written_as_seven(self):
        """Cron allows both, and a routine set for 7 that never fires is a
        bug nobody finds for a week."""
        assert parse("0 9 * * 7").weekdays == parse("0 9 * * 0").weekdays

    @pytest.mark.parametrize("line", [
        "60 * * * *",        # minute 60
        "* 24 * * *",        # hour 24
        "* * 32 * *",        # day 32
        "* * * 13 *",        # month 13
        "* * * * 8",         # weekday 8
        "* * * * -1",
        "17-3 * * * *",      # backwards
        "*/0 * * * *",       # step of zero
        "*/x * * * *",
        "0 0 0 * *",         # day zero
        "a * * * *",
        "0,, * * * *",
    ])
    def test_nonsense_in_a_field_is_refused(self, line):
        with pytest.raises(ScheduleError):
            parse(line)


class TestWhenItNextFires:

    def test_later_today(self):
        assert parse("07:30").next_after(at(2026, 9, 18, 7, 0)) \
            == at(2026, 9, 18, 7, 30)

    def test_tomorrow_when_today_has_gone(self):
        assert parse("07:30").next_after(at(2026, 9, 18, 8, 0)) \
            == at(2026, 9, 19, 7, 30)

    def test_strictly_after_never_the_same_minute(self):
        """Or a routine that just ran is due again immediately, forever."""
        assert parse("07:30").next_after(at(2026, 9, 18, 7, 30)) \
            == at(2026, 9, 19, 7, 30)

    def test_seconds_are_dropped(self):
        moment = datetime(2026, 9, 18, 7, 29, 41, 500000, tzinfo=timezone.utc)
        assert parse("07:30").next_after(moment) == at(2026, 9, 18, 7, 30)

    def test_across_midnight(self):
        assert parse("0 0 * * *").next_after(at(2026, 9, 18, 23, 59)) \
            == at(2026, 9, 19, 0, 0)

    def test_across_the_end_of_a_month(self):
        assert parse("0 9 * * *").next_after(at(2026, 9, 30, 10, 0)) \
            == at(2026, 10, 1, 9, 0)

    def test_across_the_end_of_a_year(self):
        assert parse("0 0 * * *").next_after(at(2026, 12, 31, 12, 0)) \
            == at(2027, 1, 1, 0, 0)

    def test_the_leap_day(self):
        assert parse("0 0 29 2 *").next_after(at(2027, 3, 1)) \
            == at(2028, 2, 29)

    def test_a_date_that_does_not_exist_returns_none(self):
        """0 0 30 2 * is legal cron and fires never. Saying so beats looping."""
        assert parse("0 0 30 2 *").next_after(at(2026, 1, 1)) is None

    def test_weekday_only(self):
        # 18 September 2026 is a Friday; the next Monday is the 21st.
        assert parse("0 9 * * mon").next_after(at(2026, 9, 18, 12, 0)) \
            == at(2026, 9, 21, 9, 0)

    def test_day_of_month_and_day_of_week_are_or_not_and(self):
        """Vixie's rule, and the one everybody gets wrong.

        `0 0 13 * 5` is the thirteenth OR any Friday - not Friday the
        thirteenth. Read as AND, a routine set for both would fire roughly
        twice a year instead of five times a month.
        """
        schedule = parse("0 0 13 * 5")
        # The 13th of September 2026 is a Sunday: it still fires.
        assert schedule.matches_day(datetime(2026, 9, 13).date())
        # And so does the Friday that is not the 13th.
        assert schedule.matches_day(datetime(2026, 9, 18).date())
        # A Tuesday that is not the 13th does not.
        assert not schedule.matches_day(datetime(2026, 9, 15).date())

    def test_a_restricted_day_with_a_star_weekday_is_and(self):
        schedule = parse("0 0 13 * *")
        assert schedule.matches_day(datetime(2026, 9, 13).date())
        assert not schedule.matches_day(datetime(2026, 9, 14).date())

    def test_several_times_a_day_in_order(self):
        schedule = parse("0 8,13,20 * * *")
        first = schedule.next_after(at(2026, 9, 18, 7, 0))
        second = schedule.next_after(first)
        third = schedule.next_after(second)
        fourth = schedule.next_after(third)
        assert [first.hour, second.hour, third.hour] == [8, 13, 20]
        assert fourth == at(2026, 9, 19, 8, 0)

    def test_the_timezone_of_the_question_is_the_timezone_of_the_answer(self):
        moment = at(2026, 9, 18, 7, 0)
        assert parse("07:30").next_after(moment).tzinfo is moment.tzinfo

    def test_it_terminates_rather_than_searching_forever(self):
        """The horizon is what stops an impossible line becoming a hang."""
        assert HORIZON_DAYS >= 366 * 4
        assert parse("0 0 31 2 *").next_after(at(2026, 1, 1)) is None


class TestDescribing:
    """What the panel shows, and what gets read out loud."""

    def test_a_daily_time(self):
        assert parse("07:30").describe() == "every day at 07:30"

    def test_hourly(self):
        """The alias reads better than the cron line it expands to."""
        assert parse("@hourly").describe() == "hourly"

    def test_on_the_hour_written_out_long(self):
        assert parse("0 * * * *").describe() == "hourly"

    def test_daily(self):
        assert parse("@daily").describe() == "daily"

    def test_anything_else_falls_back_to_the_line_itself(self):
        assert parse("*/5 9-17 * * 1-5").describe() == "*/5 9-17 * * 1-5"
