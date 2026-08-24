"""Deal alerts — mostly tests that we stay QUIET.

The radar reprices ~45 destinations across 4 holidays nightly. Anything that
alerts on "this is cheap" rather than "this got cheaper" fires every night and
gets muted within a week, which is the same as having no alerts at all.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as dbm
from app import notify
from app import opportunity as opp
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
    # the destination's real name, not its IATA code: the payload key is
    # destination_name, and reading item["name"] titled every alert "AGP"
    assert payload["destination_name"] == "Málaga"
    assert payload["title"].startswith("Málaga €350")
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
    """The mechanism, not this week's number — the cap is a config value."""
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    cfg.preferences["alerts"]["max_per_run"] = 3
    for dst in ("AGP", "BCN", "ALC", "PMI", "IBZ", "TIA", "MLA"):
        _seed(conn, cfg, dst, 300.0, "2026-08-23")
    spy = Spy()
    sent = notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                       "2026-08-23", url="http://ha/hook", poster=spy,
                       log=lambda *_: None)
    assert len(sent) == 3


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
    assert payload["title"].startswith("Málaga")


def test_a_failed_digest_keeps_the_queue_and_reports_the_failure(cfg, tmp_path):
    """It must RAISE, not return None.

    Returning None was indistinguishable from "nothing to send", so the
    daemon recorded the morning as done with alerts still pending and never
    performed the retry it promises.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    with pytest.raises(notify.NotifyError):
        notify.deliver(cfg, conn, url="http://ha/hook", poster=Spy(fail=True),
                       log=lambda *_: None)
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


def test_the_new_low_rule_measures_what_the_alert_prints(cfg, tmp_path):
    """An alert headlining the effective cost must be judged on it.

    Reviewer's case: the record is a EUR 800 TLL trip. A cheaper RIX FARE
    arrives, but RIX carries EUR 142 of logistics, so the trip totals EUR 842
    — EUR 42 dearer. Comparing fares while printing totals announced
    "lowest we've seen" for a more expensive holiday.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 800.0, "2026-08-22")                  # TLL, no logistics
    spy = Spy()
    _seed(conn, cfg, "AGP", 700.0, "2026-08-23", origin="RIX")    # cheaper fare...
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)
    lows = [c for _, c in spy.calls if c["kind"] == notify.KIND_LOW]
    assert lows == [], "a dearer trip is not a new low, however cheap its fare"

    # A genuine improvement on the TOTAL does speak. Kept above the buy
    # threshold so this exercises new_low rather than the buy rule, which
    # fires first and takes the destination's one slot for the run.
    _seed(conn, cfg, "AGP", 700.0, "2026-08-24")                  # TLL again
    spy2 = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-24"), "2026-08-24",
                url="http://ha/hook", poster=spy2, log=lambda *_: None,
                now=NOW + timedelta(days=1))
    lows = [c for _, c in spy2.calls if c["kind"] == notify.KIND_LOW]
    assert len(lows) == 1
    assert lows[0]["effective_eur"] == 700.0
    assert lows[0]["previous_eur"] == 800.0    # the total it beat, not a fare


def test_a_carried_over_price_never_buzzes_a_phone(cfg, tmp_path):
    """Stale data may be SHOWN — the UI labels it — but not pushed.

    A provider outage left yesterday's EUR 400 AGP as the best option, and
    the alerting treated it as tonight's find and sent a buy alert for a fare
    nobody had re-checked.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 400.0, "2026-08-22")
    _seed(conn, cfg, "BCN", 900.0, "2026-08-23")     # only BCN refreshes
    items = _items(cfg, conn, h, "2026-08-23")
    agp = next(i for i in items if i["destination"] == "AGP")
    assert agp["best_option"]["from_night"] == "2026-08-22", "fixture premise"

    spy = Spy()
    notify.send(cfg, conn, h, items, "2026-08-23", url="http://ha/hook",
                poster=spy, log=lambda *_: None)
    assert all(c["destination"] != "AGP" for _, c in spy.calls), \
        "a price nobody re-checked tonight must not reach a phone"



def test_a_queued_alert_expires_rather_than_announcing_a_dead_price(cfg, tmp_path):
    """The per-push cap must not turn into an indefinite hold.

    Overflow used to sit `pending` forever while dedupe blocked any newer
    alert for the same destination, so the digest could eventually announce a
    price from weeks ago.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    cfg.preferences["alerts"]["max_per_run"] = 5
    for dst in ("AGP", "BCN", "ALC", "PMI", "IBZ", "TIA", "MLA"):
        _seed(conn, cfg, dst, 300.0, "2026-08-23")
    assert notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                        "2026-08-23", log=lambda *_: None) == 7
    spy = Spy()
    notify.deliver(cfg, conn, url="http://ha/hook", poster=spy,
                   log=lambda *_: None)
    assert len(notify.pending(conn)) == 2, "the cap holds two back"

    # a later night arrives; the held-back pair is now describing old prices
    _seed(conn, cfg, "AGP", 300.0, "2026-08-30")
    dropped = notify.expire_stale(conn, "2026-08-30", cfg, log=lambda *_: None)
    assert dropped == 2
    assert notify.pending(conn) == []
    assert conn.execute("SELECT COUNT(*) c FROM alerts WHERE status='expired'"
                        ).fetchone()["c"] == 2


def test_a_missing_webhook_is_a_failure_not_a_completed_delivery(cfg, tmp_path,
                                                                 monkeypatch):
    """Otherwise a webhook configured later never flushes the backlog."""
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    with pytest.raises(notify.NotifyError):
        notify.deliver(cfg, conn, log=lambda *_: None)
    assert len(notify.pending(conn)) == 1


def test_the_daemon_does_not_mark_a_failed_morning_as_done(cfg, tmp_path):
    from app import daemon
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    conn.close()
    with pytest.raises(notify.NotifyError):
        daemon.deliver_alerts(cfg, tmp_path / "n.db", "2026-08-23",
                              log=lambda *_: None)
    conn = dbm.init_db(tmp_path / "n.db")
    assert not daemon.already_ran(conn, "2026-08-23", kind="alerts"), \
        "an unsent digest must leave the slot open for the retry"


def test_an_expired_alert_does_not_silence_a_fresh_one(cfg, tmp_path):
    """A message nobody received cannot be the reason for saying nothing."""
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    conn.execute("UPDATE alerts SET status='expired' WHERE status='pending'")
    conn.commit()
    _seed(conn, cfg, "AGP", 350.0, "2026-08-24")
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-24"), "2026-08-24",
                url="http://ha/hook", poster=spy, log=lambda *_: None,
                now=NOW + timedelta(days=1))
    assert [c["destination"] for _, c in spy.calls] == ["AGP"]


def test_the_payload_components_sum_to_the_price_it_advertises(cfg, tmp_path):
    """Every cost the effective price contains must be in the payload.

    The origin hotel was missing from both the alert fields and the detail
    line, so the components a reader could see did not add up to the number
    in the headline.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    o = Observation(origin="HEL", destination="AGP",
                    out_date=date(2026, 10, 26), back_date=date(2026, 11, 1),
                    price_adult_eur=50.0, source="ryanair", observed_at=NOW,
                    price_basis="quoted_rt", estimated_family_eur=200.0,
                    is_direct=True,
                    raw={"airlines": ["Ryanair"],
                         "times": {"out_departure": "2026-10-26T10:00",
                                   "out_arrival": "2026-10-26T14:00",
                                   "in_departure": "2026-11-01T19:00",
                                   "in_arrival": "2026-11-01T23:55"}})
    dbm.upsert_observations(conn, h.id, [o], seats=4, night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "HEL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])
    spy = Spy()
    notify.send(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"), "2026-08-23",
                url="http://ha/hook", poster=spy, log=lambda *_: None)
    p = spy.calls[0][1]
    # 10:00 out (before HEL's 12:00) AND 23:55 back: two separate nights
    assert p["origin_hotel_eur"] == cfg.origin("HEL").hotel_eur * 2
    assert "hotel night" in p["detail"]
    components = (p["flights_eur"] + (p["logistics_eur"] or 0)
                  + (p["layover_hotel_eur"] or 0) + (p["origin_hotel_eur"] or 0))
    assert round(components, 2) == p["effective_eur"]


def test_new_best_also_refuses_a_carried_over_price(cfg, tmp_path):
    """The stale guard lived only in the buy/new-low loop.

    Previous best was BCN; AGP's EUR 400 is a night old and only BCN
    refreshed. A genuine new_best AGP alert went out carrying
    from_night=2026-08-22.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "BCN", 500.0, "2026-08-22")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-22"),
                 "2026-08-22", log=lambda *_: None)
    conn.execute("UPDATE alerts SET status='sent', delivered=1")
    conn.commit()

    _seed(conn, cfg, "AGP", 400.0, "2026-08-22")     # stale by tonight
    _seed(conn, cfg, "BCN", 900.0, "2026-08-23")     # only BCN refreshes
    items = _items(cfg, conn, h, "2026-08-23")
    agp = next(i for i in items if i["destination"] == "AGP")
    assert agp["best_option"]["from_night"] == "2026-08-22", "fixture premise"

    spy = Spy()
    notify.send(cfg, conn, h, items, "2026-08-23", url="http://ha/hook",
                poster=spy, log=lambda *_: None, now=NOW + timedelta(days=1))
    for _, c in spy.calls:
        assert not c.get("from_night"), f"stale alert sent: {c['kind']} {c['destination']}"


def test_a_fresh_option_still_alerts_when_a_stale_one_outranks_it(cfg, tmp_path):
    """Skipping the whole destination would silence real news.

    Yesterday's Ryanair EUR 400 outranks tonight's airBaltic EUR 500, but the
    EUR 500 is a genuine fresh find and must still be announced.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 26), date(2026, 11, 1)

    def obs(source, fam, night):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source=source, observed_at=NOW,
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=True, raw={"airlines": [source]})], seats=4, night=night)

    obs("ryanair", 400.0, "2026-08-22")
    obs("airbaltic", 500.0, "2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])

    items = _items(cfg, conn, h, "2026-08-23")
    agp = items[0]
    assert agp["cheapest_option"]["effective_eur"] == 400.0     # stale, shown
    assert agp["best_fresh_option"]["effective_eur"] == 500.0   # tonight's

    spy = Spy()
    notify.send(cfg, conn, h, items, "2026-08-23", url="http://ha/hook",
                poster=spy, log=lambda *_: None)
    assert [c["effective_eur"] for _, c in spy.calls] == [500.0]


def test_a_quiet_day_without_a_webhook_is_not_a_failure(cfg, tmp_path,
                                                        monkeypatch):
    """Checking the URL before the queue turned nothing-to-say into an error.

    The daemon then retried every 30 minutes for the rest of the day over an
    empty queue.
    """
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    conn = dbm.init_db(tmp_path / "n.db")
    assert notify.deliver(cfg, conn, log=lambda *_: None) is None

    # ...but a queue with nowhere to go IS a failure worth retrying
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, "AGP", 350.0, "2026-08-23")
    notify.queue(cfg, conn, h, _items(cfg, conn, h, "2026-08-23"),
                 "2026-08-23", log=lambda *_: None)
    with pytest.raises(notify.NotifyError):
        notify.deliver(cfg, conn, log=lambda *_: None)


def test_describe_carries_freshness_so_assertions_are_not_vacuous():
    """The payload must expose from_night.

    A test asserting "no alert carries from_night" passed while stale alerts
    went out, because describe() never put the field in the payload. An
    assertion about a key nobody writes proves nothing.
    """
    from app.opportunity import deal_label  # noqa: F401  (import sanity)
    cfg = load_config(ROOT / "config.yaml")
    h = cfg.holiday("autumn-2026")
    item = {"destination": "AGP", "destination_name": "Málaga", "climate": {}}
    opt = {"origin": "TLL", "effective_eur": 400.0, "flights_eur": 400.0,
           "out_date": "2026-10-26", "back_date": "2026-11-01", "nights": 6,
           "airlines": ["Ryanair"], "is_direct": True, "school_days": 0,
           "from_night": "2026-08-22"}
    d = notify.describe(cfg, h, item, opt)
    assert d["from_night"] == "2026-08-22"
    assert notify.describe(cfg, h, item, {**opt, "from_night": None})[
        "from_night"] is None


def test_a_cheap_connection_still_alerts_when_a_dearer_nonstop_outscores_it(
        cfg, tmp_path):
    """The buy rule is about price, so it must see the cheapest fresh option.

    Handing every rule the highest-SCORING fresh candidate collapsed the full
    set back to one wrong answer: a EUR 600 connection under the buy
    threshold went unannounced because a EUR 700 nonstop outscored it.
    """
    conn = dbm.init_db(tmp_path / "n.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 26), date(2026, 11, 1)
    for fam, direct, src in ((700.0, True, "airbaltic"),
                             (600.0, False, "google_flights")):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source=src, observed_at=NOW,
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=direct, raw={"airlines": [src]})], seats=4,
            night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])

    items = _items(cfg, conn, h, "2026-08-23")
    assert items[0]["best_fresh_option"]["effective_eur"] == 700.0
    assert items[0]["cheapest_fresh_option"]["effective_eur"] == 600.0

    spy = Spy()
    notify.send(cfg, conn, h, items, "2026-08-23", url="http://ha/hook",
                poster=spy, log=lambda *_: None)
    assert [c["effective_eur"] for _, c in spy.calls] == [600.0]
