"""Eén live-verzender: HA laat live aan de plugin zolang die hartslag geeft."""
import time

import pytest

from custom_components.be_mijnbatterij import calc
from custom_components.be_mijnbatterij.api import BEError, MBError
from custom_components.be_mijnbatterij.engine import Engine


class BE:
    async def results(self, *a):
        raise BEError("geen")


class MB:
    def __init__(self, recs):
        self.recs, self.live_log, self.post = recs, [], []

    async def dagen(self, van, tot):
        return self.recs

    async def live(self, p):
        self.live_log.append(p)

    async def dag(self, p):
        self.post.append(p)


def engine(recs, nu):
    e = Engine(BE(), MB(recs), {"installatie": "X"}, {"versturen": True, "proef": False}, "t", now_ms=lambda: nu)
    e.s["laatste"] = {"t": nu - 10_000, "po": 500, "bo": 1000, "s": 50}
    return e


@pytest.mark.parametrize("minuten_geleden,verwacht_live", [(5, False), (40, True)])
async def test_live_alleen_zonder_browserhartslag(minuten_geleden, verwacht_live):
    nu = int(time.time() * 1000)
    gister = calc.plus_dagen(calc.vandaag(nu), -1)
    label = f"[BEMB v=1.2.4 bron=browser type=cache dekking=100.0 actief={calc.iso_ams(nu - minuten_geleden * 60_000)}]"
    e = engine({gister: {"date": gister, "note": "x " + label}}, nu)
    assert await e.heartbeat() is None  # geen dag om op te schrijven
    assert e.hb_mislukt and e.browser_hartslag == pytest.approx(nu - minuten_geleden * 60_000, abs=1000)
    if verwacht_live:
        await e.live()
        assert e.mb.live_log
    else:
        with pytest.raises(MBError, match="browserplugin"):
            await e.live()
        assert not e.mb.live_log


async def test_zonder_browser_gewoon_live():
    nu = int(time.time() * 1000)
    e = engine({}, nu)
    await e.heartbeat()
    await e.live()
    assert e.mb.live_log
