import pytest

from photokeep.metadata import derive, fingerprint, provider_record


def test_exif_date_with_offset():
    m = derive({"DateTimeOriginal": "2024:01:04 11:34:00", "OffsetTimeOriginal": "-06:00"}, None, "a.jpg", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "exif")


def test_exif_wins_over_everything():
    exif = {"DateTimeOriginal": "2024:01:04 11:34:00", "CreationDate": "2020:01:01 00:00:00+00:00"}
    m = derive(exif, provider_record(taken="2019-01-01T00:00:00Z"), "20180101_000000.jpg", "2017-01-01T00:00:00Z")
    assert m["taken"] == "2024-01-04T11:34:00"
    assert m["tz"] is None


def test_quicktime_creation_date_keeps_its_offset():
    m = derive({"CreationDate": "2024:01:04 11:34:00-06:00", "CreateDate": "2000:01:01 00:00:00"}, None, "a.mov", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "quicktime")


def test_quicktime_create_date_is_utc_shown_local(mexico):
    m = derive({"CreateDate": "2024:01:04 17:34:00"}, None, "a.mov", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "quicktime")


@pytest.mark.parametrize("exif, name", [({"MIMEType": "image/jpeg"}, "a.jpg"), ({"MIMEType": "image/heic"}, "a.HEIC"),
                                         ({}, "a.png")])
def test_photo_create_date_is_the_cameras_wall_clock(mexico, exif, name):
    m = derive(exif | {"CreateDate": "2024:01:04 11:34:00"}, None, name, None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", None, "exif")


def test_photo_create_date_keeps_its_offset(mexico):
    m = derive({"CreateDate": "2024:01:04 11:34:00", "OffsetTimeDigitized": "+02:00"}, None, "a.jpg", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "+02:00", "exif")


@pytest.mark.parametrize("exif, name", [({"MIMEType": "video/quicktime"}, "a.MOV"), ({"MIMEType": "video/mp4"}, "a.mov"),
                                         ({}, "a.mp4")])
def test_video_create_date_is_utc_shown_local(mexico, exif, name):
    m = derive(exif | {"CreateDate": "2024:01:04 17:34:00"}, None, name, None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "quicktime")


def test_provider_date_is_utc_shown_local(mexico):
    m = derive({}, provider_record(taken="2024-01-04T17:34:00Z"), "a.jpg", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "provider")


def test_provider_date_comes_after_quicktime_and_before_the_name(mexico):
    provider = provider_record(taken="2024-01-04T17:34:00Z")
    assert derive({"CreationDate": "2020:01:01 00:00:00+00:00"}, provider, "a.mov", None)["date_src"] == "quicktime"
    assert derive({}, provider, "20180101_000000.jpg", None)["date_src"] == "provider"


def test_provider_place_only_when_exif_has_none():
    provider = provider_record(lat=19.4, lon=-99.1)
    m = derive({}, provider, "a.jpg", None)
    assert (m["lat"], m["lon"]) == (19.4, -99.1)
    m = derive({"GPSLatitude": 1.5, "GPSLongitude": -2.5}, provider, "a.jpg", None)
    assert (m["lat"], m["lon"]) == (1.5, -2.5)
    assert derive({}, {}, "a.jpg", None)["lat"] is None


def test_provider_record_has_the_shared_keys():
    assert provider_record(taken="2024-01-04T17:34:00Z", albums=("Trip",), raw={"x": 1}) == {
        "taken": "2024-01-04T17:34:00Z", "lat": None, "lon": None, "title": None, "description": None,
        "favorite": False, "hidden": False, "albums": ["Trip"], "memories": [], "raw": {"x": 1}}


@pytest.mark.parametrize("name", ["IMG 2024-01-04 11.34.56.jpg", "2024-01-04_11.34.56.jpg", "20240104_113456.jpg"])
def test_filename_pattern(name):
    m = derive({}, None, name, None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:56", None, "filename")


def test_mtime_is_the_last_resort(mexico):
    m = derive({}, None, "a.jpg", "2024-01-04T17:34:00Z")
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "mtime")


def test_unknown_date():
    m = derive({}, None, "a.jpg", None)
    assert (m["taken"], m["tz"], m["date_src"]) == (None, None, None)


def test_year_zero_and_invalid_dates_fall_through():
    exif = {"DateTimeOriginal": "0000:00:00 00:00:00", "CreationDate": "2024:13:45 11:34:00+00:00"}
    m = derive(exif, None, "20240104_113456.jpg", None)
    assert m["date_src"] == "filename"


def test_kind_and_fields():
    exif = {"MIMEType": "video/quicktime", "ImageWidth": 1920, "ImageHeight": 1080, "Duration": 3.2,
            "CompressorID": "hvc1", "Make": "Apple", "Model": "iPhone 15", "GPSLatitude": 1.5, "GPSLongitude": -2.5}
    m = derive(exif, None, "a.mov", None)
    assert (m["kind"], m["w"], m["h"], m["duration"], m["codec"]) == ("video", 1920, 1080, 3.2, "hvc1")
    assert (m["make"], m["model"], m["lat"], m["lon"]) == ("Apple", "iPhone 15", 1.5, -2.5)
    assert derive({}, None, "a.MP4", None)["kind"] == "video"
    assert derive({"MIMEType": "image/jpeg"}, None, "a.jpg", None)["kind"] == "photo"


def test_screenshot_rule():
    assert derive({"UserComment": "Screenshot", "Model": "x"}, None, "a.jpg", None)["screenshot"]
    assert derive({"FileType": "PNG"}, None, "a.png", None)["screenshot"]
    assert not derive({"FileType": "PNG", "Model": "x"}, None, "a.png", None)["screenshot"]
    assert not derive({}, None, "a.jpg", None)["screenshot"]


def test_fingerprint():
    meta = {"taken": "2024-01-04T11:34:00", "model": "iPhone 15", "w": 4032, "h": 3024}
    assert fingerprint(meta) == ("2024-01-04T11:34:00", "iPhone 15", 4032, 3024)
    assert fingerprint({**meta, "model": None}) is None
    assert fingerprint({**meta, "taken": None}) is None
