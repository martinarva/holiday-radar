"""Deal alerts — mostly tests that we stay QUIET.

The radar reprices ~45 destinations across 4 holidays nightly. Anything that
alerts on "this is cheap" rather than "this got cheaper" fires every night and
gets muted within a week, which is the same as having no alerts at all.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as dbm, notify, opportunity as opp
from app.config import load_config
from app.providers.base import Observation

ROOT = Path(__file__).parent.parent
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def cfg():
    return load_config(ROOT / "config.yaml")


class Spy:
    """Stands in for the webhook so no test touches the network."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, payload, timeout=15):
        if self.fail:
            raise notify.NotifyError("boom")
        self.calls.append((url, payload))
        return 200


def _seed(conn, cfg, dst, family, night, out=date(2026, 10, 26),
          back=date(2026, 11, 1), direct=True, origin="TLL"):
    o = Observation(origin=origin, destination=dst, out_date=out, back_date=back,
                    price_adult_eur=round(family / 4, 2), source="airbaltic",
                    observed_at=datetime.fromisoformat(night + "T03:00:00+00:00"),
                    price_basis="family_quote", estimated_family_eur=family,
                    is_direct=direct, raw={"airlines": ["airBaltic"]})
    dbm.upsert_observations(conn, "autumn-2026", [o], seats=4)
    dbm.write_watch_state(conn, [{
        "holiday_id": "autumn-2026", "origin": origin, "destination": dst,
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])


def _items(cfg, conn, h, night):
    return opp.build(cfg, conn, h, night=night, climate_cache={})


def test_a_deal_under_the_buy_threshold_is_announced(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")        # short tier: notify 400
    spy = Spy()
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy, log=lambda *_: None)
    assert len(sent) == 1
    kind, payload = sent[0]["kind"], spy.calls[0][1]
    assert kind == notify.KIND_BUY
    assert payload["destination"] == "AGP"
    assert payload["effective_eur"] == 350.0
    assert payload["deal"] in ("good", "exceptional")
    assert "AGP" in payload["title"] or "Malaga" in payload["title"]
    # the payload must carry enough to act on without opening the app
    for k in ("out_date", "back_date", "nights", "origin", "airlines",
              "school_days", "detail", "confidence"):
        assert k in payload


def test_the_same_price_the_next_night_says_nothing(cfg, tmp_path):
    """The whole point. A fare that holds is not news."""
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)
    assert len(spy.calls) == 1

    _seed(conn, cfg, "AGP", 350.0, "2026-08-24")
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-24"), "2026-08-24",
                url="http://ha/hook", poster=spy, log=lambda *_: None,
                now=NOW + timedelta(days=1))
    assert len(spy.calls) == 1, "a price that merely holds must stay silent"


def test_a_trivial_wobble_stays_silent_but_a_real_drop_speaks(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)

    _seed(conn, cfg, "AGP", 340.0, "2026-08-24")        # -10 EUR, under both floors
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-24"), "2026-08-24",
                url="http://ha/hook", poster=spy, log=lambda *_: None,
                now=NOW + timedelta(days=1))
    assert len(spy.calls) == 1, "a EUR 10 wobble is not worth a phone buzz"

    _seed(conn, cfg, "AGP", 280.0, "2026-08-25")        # -70 EUR and -20%
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-25"), "2026-08-25",
                url="http://ha/hook", poster=spy, log=lambda *_: None,
                now=NOW + timedelta(days=2))
    assert len(spy.calls) == 2
    assert spy.calls[1][1]["effective_eur"] == 280.0
    assert spy.calls[1][1]["previous_eur"] == 350.0


def test_an_expensive_destination_never_alerts_on_price_alone(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 1800.0, "2026-08-23")       # far above the threshold
    spy = Spy()
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy,
                       log=lambda *_: None)
    assert [a for a in sent if a["kind"] == notify.KIND_BUY] == []


def test_a_new_all_time_low_is_news_even_above_the_threshold(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    for night, price in (("2026-08-21", 900.0), ("2026-08-22", 880.0)):
        _seed(conn, cfg, "AGP", price, night)
    spy = Spy()
    _seed(conn, cfg, "AGP", 700.0, "2026-08-23")        # below every prior night
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy,
                       log=lambda *_: None)
    lows = [a for a in sent if a["kind"] == notify.KIND_LOW]
    assert len(lows) == 1
    assert lows[0]["previous_eur"] == 880.0


def test_no_webhook_configured_is_not_an_error(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    assert notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", log=lambda *_: None) == []


def test_a_failed_push_never_aborts_the_night_or_is_recorded_as_sent(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=Spy(fail=True),
                       log=lambda *_: None)
    assert sent == []
    # a baseline row may exist, but nothing may be marked as having reached a
    # phone -- otherwise the failed alert is silently lost forever
    assert conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE delivered=1").fetchone()["c"] == 0
    # ...so the next night can still deliver it
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)
    assert len(spy.calls) == 1


def test_the_per_run_cap_holds(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    for dst in ("AGP", "BCN", "ALC", "PMI", "IBZ", "TIA", "MLA"):
        _seed(conn, cfg, dst, 300.0, "2026-08-23")
    spy = Spy()
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy,
                       log=lambda *_: None)
    assert len(sent) == cfg.preferences["alerts"]["max_per_run"] == 5


def test_alerts_can_be_switched_off_without_touching_the_env(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    cfg.preferences["alerts"]["enabled"] = False
    spy = Spy()
    assert notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy,
                       log=lambda *_: None) == []
    assert spy.calls == []


def test_an_overnight_layover_is_disclosed_in_the_alert(cfg, tmp_path):
    """A cheap fare that needs a hotel must say so on the phone, not in the app."""
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 250.0, "2026-08-23", direct=False)
    conn.execute("""UPDATE observations SET max_layover_h=15.42,
                    layover_label='15h25 in WAW (overnight)', layover_overnight=1""")
    conn.commit()
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)
    p = spy.calls[0][1]
    assert p["layover"] == "15h25 in WAW (overnight)"
    assert p["layover_overnight"] is True
    assert "layover hotel" in p["detail"]
    assert p["effective_eur"] == 360.0       # 250 fare + 110 room


# --- the 02:45 decide / 07:00 deliver split --------------------------------

def test_the_night_queues_and_posts_nothing(cfg, tmp_path):
    """Owner wants the phone at 07:00, so 02:45 must be silent."""
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    spy = Spy()
    queued = notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                          "2026-08-23", log=lambda *_: None)
    assert queued == 1
    assert spy.calls == [], "nothing may reach a phone at 02:45"
    assert conn.execute("SELECT COUNT(*) c FROM alerts WHERE status='pending'"
                        ).fetchone()["c"] == 1


def test_the_morning_slot_sends_one_digest_for_several_finds(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    for dst, price in (("AGP", 350.0), ("BCN", 300.0), ("ALC", 380.0)):
        _seed(conn, cfg, dst, price, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    spy = Spy()
    payload = notify.deliver(cfg, conn, url="http://ha/hook", poster=spy,
                             log=lambda *_: None)
    assert len(spy.calls) == 1, "three finds, one buzz"
    assert payload["kind"] == notify.KIND_DIGEST and payload["count"] == 3
    # leads with the best price so the notification's first line is the news
    assert payload["best"]["destination"] == "BCN"
    assert "3 new flight deals" in payload["title"]
    assert len(payload["alerts"]) == 3
    assert conn.execute("SELECT COUNT(*) c FROM alerts WHERE status='pending'"
                        ).fetchone()["c"] == 0


def test_a_single_find_keeps_its_own_headline(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    payload = notify.deliver(cfg, conn, url="http://ha/hook", poster=Spy(),
                             log=lambda *_: None)
    assert payload["count"] == 1
    assert payload["title"].startswith("Malaga") or "AGP" in payload["title"]


def test_a_failed_digest_keeps_the_queue_for_the_next_morning(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    assert notify.deliver(cfg, conn, url="http://ha/hook", poster=Spy(fail=True),
                          log=lambda *_: None) is None
    assert len(notify.pending(conn)) == 1, "a failed push must not lose alerts"
    spy = Spy()
    assert notify.deliver(cfg, conn, url="http://ha/hook", poster=spy,
                          log=lambda *_: None) is not None
    assert len(spy.calls) == 1


def test_an_empty_queue_sends_nothing_at_all(cfg, tmp_path):
    """No news is no notification — not a "nothing found today" push."""
    conn = dbm.init_db(tmp_path / "n.db")
    spy = Spy()
    assert notify.deliver(cfg, conn, url="http://ha/hook", poster=spy,
                          log=lambda *_: None) is None
    assert spy.calls == []


def test_a_queued_alert_does_not_requeue_the_next_night(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    _seed(conn, cfg, "AGP", 350.0, "2026-08-24")
    assert notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-24"),
                        "2026-08-24", log=lambda *_: None,
                        now=NOW + timedelta(days=1)) == 0


def test_the_daemon_reads_the_delivery_hour_from_config(cfg):
    from app.daemon import parse_hm
    assert parse_hm(cfg.preferences["alerts"]["deliver_cron"]) == (7, 0)


def test_waking_for_the_digest_does_not_arm_the_repricing_run():
    """The daemon must know WHICH slot it woke for, not just when."""
    from app.daemon import next_slot
    nightly, alerts = (2, 45), (7, 0)

    at, which = next_slot(datetime(2026, 8, 23, 23, 0), nightly, alerts)
    assert which == "nightly" and at.hour == 2      # late evening -> 02:45

    at, which = next_slot(datetime(2026, 8, 23, 3, 0), nightly, alerts)
    assert which == "alerts" and at.hour == 7       # after the run -> 07:00

    at, which = next_slot(datetime(2026, 8, 23, 8, 0), nightly, alerts)
    assert which == "nightly" and at.day == 24      # past both -> tomorrow
