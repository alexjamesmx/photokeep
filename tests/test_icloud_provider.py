from photokeep.metadata import derive, provider_record
from photokeep.providers import icloud

HEADER = "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount\n"


def row(name, fav="no", hidden="no", date="Thursday January 4,2024 5:34 PM GMT"):
    return f'{name},abc,{fav},{hidden},no,"{date}",0\n'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def test_reads_details_flags_and_date(tmp_path):
    write(tmp_path / "part1/Photos/Photo Details.csv", HEADER + row("a.jpg", fav="yes") + row("b.jpg", hidden="yes"))
    write(tmp_path / "part2/Photos/Photo Details-1.csv", HEADER + row("c.jpg", date="garbage"))
    out = icloud.read(tmp_path)
    assert set(out) == {"part1/Photos/a.jpg", "part1/Photos/b.jpg", "part2/Photos/c.jpg"}
    a = out["part1/Photos/a.jpg"]
    assert set(a) == set(provider_record())
    assert (a["taken"], a["favorite"], a["hidden"]) == ("2024-01-04T17:34:00Z", True, False)
    assert out["part1/Photos/b.jpg"]["hidden"] is True
    assert a["raw"]["fileChecksum"] == "abc"
    assert out["part2/Photos/c.jpg"]["taken"] is None


def test_albums_memories_and_merged_titles(tmp_path):
    write(tmp_path / "part1/Photos/Photo Details.csv", HEADER + row("a.jpg") + row("b.jpg"))
    write(tmp_path / "part1/Albums/Spring 2025.csv", "Images\na.jpg\n")
    write(tmp_path / "part1/Albums/Spring 20251.csv", "Images\nb.jpg\na.jpg\n")
    write(tmp_path / "part1/Albums/Favorites.csv", "Images\na.jpg\n")
    write(tmp_path / "part1/Albums/Hidden.csv", "Images\nb.jpg\n")
    write(tmp_path / "part1/Memories/Trip.csv", "Images\nb.jpg\n")
    out = icloud.read(tmp_path)
    assert out["part1/Photos/a.jpg"]["albums"] == ["Spring 2025"]
    assert out["part1/Photos/b.jpg"]["albums"] == ["Spring 2025"]
    assert out["part1/Photos/b.jpg"]["memories"] == ["Trip"]
    assert out["part1/Photos/a.jpg"]["memories"] == []


def test_damaged_export_gives_what_it_can(tmp_path):
    huge = '"' + "x" * 200_000 + '"\n'  # over csv.field_size_limit()
    write(tmp_path / "part1/Photos/Photo Details.csv", HEADER + row("a.jpg") + "b.jpg,abc\n" + huge + row("c.jpg"))
    write(tmp_path / "part1/Albums/Trip.csv", "Images\na.jpg\n" + huge + "c.jpg\n")
    (tmp_path / "part2/Photos").mkdir(parents=True)
    (tmp_path / "part2/Photos/Photo Details-1.csv").write_bytes(b"imgName\n\xff\xfe\n")
    out = icloud.read(tmp_path)
    assert set(out) == {"part1/Photos/a.jpg", "part1/Photos/b.jpg"}
    assert out["part1/Photos/b.jpg"]["taken"] is None
    assert out["part1/Photos/a.jpg"]["albums"] == ["Trip"]


def test_migrated_photo_shows_the_date_it_showed_before(tmp_path, mexico):
    # A photo whose only date is in Photo Details, read the way the viewer reads the migrated library.
    write(tmp_path / "part1/Photos/Photo Details.csv", HEADER + row("a.jpg"))
    m = derive({}, icloud.read(tmp_path)["part1/Photos/a.jpg"], "a.jpg", None)
    assert (m["taken"], m["tz"], m["date_src"]) == ("2024-01-04T11:34:00", "-06:00", "provider")
