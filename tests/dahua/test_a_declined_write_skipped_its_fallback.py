"""Two fallbacks that could not hear the refusal they were written for.

Reported by @clore-hub on #1115, against a VTO4202F-P-S2 on firmware 4.600,
with both calls made by hand:

    setConfig&MotionDetect[0].Enable=false&MotionDetect[0].DetectVersion=V3.0
      -> Error
    setConfig&MotionDetect[0].Enable=false
      -> OK, and getConfig shows it took

`getConfig&name=MotionDetect` on that firmware carries no `DetectVersion` key
at all, so the two-key form can never work there. `_enable_motion_detection_cgi`
has had a fallback to the one-key form since #921, and it never ran:

    response = await self.get(url)
    if "OK" in response:          # unreachable for this device
        return response

Writes go through `get()` with `reject_declined`, and a 200 carrying `Error`
raises `DahuaWriteDeclined` from there. The first `await` left the function,
the switch raised, and motion detection could not be turned off.

**The existing tests passed throughout**, because the double they use replaces
`client.get` with a function that *returns* "Error". That is the shape the code
was written against and not the one it meets, so the tests agreed with the code
and both were wrong about the device. There is a test at the bottom of this file
that ties the double to the real `get`, so it cannot drift back.

The same omission is in `async_set_video_in_day_night_mode`'s fallback,
which caught `ClientResponseError` only. `DahuaWriteDeclined` is a
`ConnectionError`, so it went straight past.
"""

import aiohttp
import pytest

from custom_components.dahua.client import (
    DahuaClient,
    DahuaWriteDeclined,
    write_was_declined,
)


class _Sent:
    """Records the URLs and answers with whatever the test chose.

    An answer that is an exception is raised, which is how `get` really
    reports a declined write.
    """

    def __init__(self, answers):
        self.urls = []
        self.answers = list(answers)

    async def get(self, url, verify_response=False):
        self.urls.append(url)
        answer = self.answers.pop(0) if self.answers else "OK"
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _client(*answers):
    client = object.__new__(DahuaClient)
    client._address = "10.0.0.5"
    sent = _Sent(answers)
    client.get = sent.get
    client._sent = sent
    return client


def _declined():
    return DahuaWriteDeclined("10.0.0.5 declined the write: Error")


# --- the reported fault -------------------------------------------------------


async def test_a_raised_refusal_still_reaches_the_fallback():
    """@clore-hub's VTO. The first form is refused as an exception, and the
    one-key form is what the device accepts."""
    client = _client(_declined(), "OK")

    await client._enable_motion_detection_cgi(0, False)

    assert len(client._sent.urls) == 2, client._sent.urls
    second = client._sent.urls[1]
    assert "MotionDetect[0].Enable=false" in second, second
    assert "DetectVersion" not in second, second


async def test_the_fallback_s_answer_is_returned():
    client = _client(_declined(), "OK second time")

    assert await client._enable_motion_detection_cgi(0, False) == "OK second time"


async def test_a_device_declining_both_forms_says_so():
    """Not swallowed. If neither form is accepted the user has to be told, and
    the second refusal is the honest one to report: it is the simplest request
    the device could have been asked."""
    client = _client(_declined(), _declined())

    with pytest.raises(DahuaWriteDeclined):
        await client._enable_motion_detection_cgi(0, False)

    assert len(client._sent.urls) == 2


# --- the shape that already worked, kept --------------------------------------


async def test_a_body_that_is_simply_not_ok_still_falls_back():
    """The original fallback. A device can answer something that is neither OK
    nor the word Error, and that is still not consent."""
    client = _client("Unknown error", "OK")

    await client._enable_motion_detection_cgi(5, True)

    assert len(client._sent.urls) == 2, client._sent.urls
    assert "DetectVersion" not in client._sent.urls[1]


async def test_a_device_that_accepts_the_two_key_form_is_asked_once():
    """The negative control. Most cameras take the first form, and asking every
    one of them twice would double the writes for nothing."""
    client = _client("OK")

    await client._enable_motion_detection_cgi(5, True)

    assert len(client._sent.urls) == 1, client._sent.urls
    assert "MotionDetect[5].DetectVersion=V3.0" in client._sent.urls[0]


@pytest.mark.parametrize("enabled", [True, False])
async def test_both_directions_reach_the_fallback(enabled):
    """#1115 is about turning it off, but nothing in the refusal is about the
    value, so on would have failed the same way."""
    client = _client(_declined(), "OK")

    await client._enable_motion_detection_cgi(0, enabled)

    assert "Enable={0}".format(str(enabled).lower()) in client._sent.urls[1]


# --- the same omission, in the day/night fallback -----------------------------


async def test_the_day_night_fallback_hears_a_declined_write():
    """`VideoInDayNight` refused with a 200 and `Error` went straight past a
    handler that named `ClientResponseError` only, so `VideoInOptions`, which
    is what those devices actually carry, was never tried."""
    client = _client(_declined(), "OK")

    await client.async_set_video_in_day_night_mode(0, "general", "Color")

    assert len(client._sent.urls) == 2, client._sent.urls
    assert "VideoInOptions[0].DayNightColor" in client._sent.urls[1]


async def test_the_day_night_fallback_still_hears_a_status():
    """The half that was already caught, kept."""
    client = _client(
        aiohttp.ClientResponseError(None, None, status=400, message="x"), "OK"
    )

    await client.async_set_video_in_day_night_mode(0, "general", "Color")

    assert len(client._sent.urls) == 2, client._sent.urls


# --- and the double is the real thing -----------------------------------------


def test_get_really_raises_for_the_body_these_tests_feed():
    """The guard that keeps this file honest.

    Every test above hands the double a `DahuaWriteDeclined` and claims that is
    what a device answering `Error` produces. If that stopped being true the
    tests would keep passing against a shape the code never meets, which is
    exactly how the fallback stayed broken through two releases while its own
    tests were green.

    Asserted against the predicate `get` uses rather than against a copy of the
    string, so the two cannot drift apart.
    """
    assert write_was_declined("Error")
    assert write_was_declined("error\n")
    assert not write_was_declined("OK")
    assert not write_was_declined("")
