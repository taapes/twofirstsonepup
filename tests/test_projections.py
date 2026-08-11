"""Importing an outside analyst's point projections.

A draft is prepared in the offseason, when `players` holds nothing but zeros, so the
Players tab ranks on EXPECTED points. The numbers come from a spreadsheet, which means
the risk is not arithmetic — it is attributing one human's projection to another, or
reading a column that isn't the column you think it is. These tests are about the
matching and the parsing.

The pure ones need no database; the rest use TEST_DATABASE_URL (see conftest).
"""

import os
import sys
import uuid
import xml.etree.ElementTree as ET

import pytest
from sqlalchemy.exc import IntegrityError

import services
from models import League, Player, PlayerProjection

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "scripts"))
import import_projections as ip  # noqa: E402


# ---- normalization --------------------------------------------------------
@pytest.mark.parametrize("sheet,pool", [
    ("Odegaard", "Ødegaard"),
    ("Norgaard", "Nørgaard"),
    ("F.Kadioglu", "F.Kadıoğlu"),
    ("Hjerto-Dahl", "Hjertø-Dahl"),
])
def test_norm_bridges_the_characters_nfkd_drops(sheet, pool):
    """ø/ı have no NFKD decomposition, so an ascii-ignore pass DELETES them and
    'Ødegaard' becomes 'degaard'. The translation table looks like decoration; without
    it these four players silently lose their projections."""
    assert ip._norm(sheet) == ip._norm(pool)


def test_norm_lowercases_before_transliterating():
    """Order matters: the translation table has lowercase keys only, so an uppercase
    Ø would slip past it and then be deleted by ascii-ignore."""
    assert ip._norm("ØDEGAARD") == ip._norm("odegaard")


def test_norm_strips_punctuation_and_spacing():
    assert ip._norm("B.Fernandes") == ip._norm("B Fernandes") == "bfernandes"


# ---- xlsx parsing ---------------------------------------------------------
def _sheet_xml(rows):
    """rows: [[(cell ref, type, text), ...]] -> a worksheet element."""
    body = ""
    for i, cells in enumerate(rows, start=1):
        cs = "".join(
            f'<c r="{ref}"{f" t={t!r}" if t else ""}><v>{val}</v></c>'
            for ref, t, val in cells
        )
        body += f'<row r="{i}">{cs}</row>'
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return ET.fromstring(f'<worksheet xmlns="{ns}"><sheetData>{body}</sheetData></worksheet>')


def _cells_from(rows, shared=None):
    """Build a real xlsx and run it through read_sheet.

    Deliberately not a reimplementation of the cell walk: the parser's one silent
    corruption path is reading cells by position instead of by column letter, and a
    test that rebuilds that logic itself can never catch it.
    """
    import tempfile
    import zipfile
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = ET.tostring(_sheet_xml(rows), encoding="unicode")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        path = f.name
    with zipfile.ZipFile(path, "w") as z:
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    try:
        return ip.read_sheet(path)
    finally:
        os.unlink(path)


def _header_row():
    return [(f"{chr(ord('A') + i)}1", None, h) for i, h in enumerate(ip.HEADERS)]


def _data_row(n=2, **over):
    vals = {"A": "Haaland", "B": "MCI", "C": "FWD", "D": "15.5", "E": "3120",
            "F": "27.5", "G": "5.8", "H": "0", "I": "34.7", "J": "0.4",
            "K": "3.2", "L": "231", "M": "14.9"}
    vals.update(over)
    return [(f"{c}{n}", None, v) for c, v in vals.items()]


def test_cells_are_read_by_column_letter_not_position():
    """Excel omits empty cells entirely. Read positionally and one blank CS shifts
    every later column by one — DC lands in `bonus` — with no error anywhere."""
    row = [c for c in _data_row() if not c[0].startswith("H")]  # CS cell absent
    cells = _cells_from([_header_row(), row])
    rows, _ = ip.parse_rows(cells)
    assert rows[0]["clean_sheets"] is None
    assert rows[0]["bonus"] == 34.7, "a shifted column silently corrupted the row"
    assert rows[0]["defensive_contributions"] == 0.4
    assert rows[0]["points"] == 231.0


def test_a_changed_header_row_is_refused():
    """The header check is what makes reading by column letter trustworthy."""
    swapped = _header_row()
    swapped[5] = ("F1", None, "xG")
    with pytest.raises(ValueError, match="header"):
        ip.parse_rows(_cells_from([swapped, _data_row()]))


def test_shared_string_runs_are_joined(tmp_path):
    """`si.find(t)` returns only the FIRST run, so one bolded surname in a re-export
    would truncate a name to a prefix that then matches nothing — and reads like a
    data problem rather than a parser bug."""
    import zipfile
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = (f'<sst xmlns="{ns}"><si><t>Hjertø</t><t>-Dahl</t></si>'
              f'<si><t>HUL</t></si><si><t>MID</t></si></sst>')
    cells = [("A2", "s", "0"), ("B2", "s", "1"), ("C2", "s", "2")]
    cells += [(f"{c}2", None, "1") for c in "DEFGHIJKLM"]
    sheet = ET.tostring(_sheet_xml([_header_row(), cells]), encoding="unicode")
    path = tmp_path / "s.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)

    rows, _ = ip.parse_rows(ip.read_sheet(str(path)))
    assert rows[0]["raw_name"] == "Hjertø-Dahl"


def test_team_and_position_fixups_are_applied_and_counted():
    """A fixup that fires on zero rows is the loudest signal the sheet's code set
    changed under us, so the counts are part of the report."""
    row = _data_row(B="BRI", C="GK")
    rows, fixups = ip.parse_rows(_cells_from([_header_row(), row]))
    assert rows[0]["match_team"] == "BHA" and rows[0]["match_position"] == "GKP"
    assert rows[0]["raw_team"] == "BRI", "the sheet's own value must be kept verbatim"
    assert fixups == {"team": 1, "position": 1}

    plain = ip.parse_rows(_cells_from([_header_row(), _data_row()]))
    assert plain[1] == {"team": 0, "position": 0}


# ---- matching -------------------------------------------------------------
def _pool(*rows):
    return [{"id": uuid.uuid4(), "name": n, "position": p, "current_team": t,
             "price": 50} for n, p, t in rows]


def test_a_player_is_matched_on_name_position_and_team():
    pool = _pool(("Haaland", "FWD", "MCI"))
    rows, _ = ip.parse_rows(_cells_from([_header_row(), _data_row()]))
    matched, unmatched, ambiguous = ip.resolve(rows, ip.build_index(pool))
    assert len(matched) == 1 and not unmatched and not ambiguous
    assert matched[0]["player_id"] == str(pool[0]["id"])


def test_a_duplicate_name_is_disambiguated_by_position_and_team():
    """The live pool has 15 duplicate `name` values and zero duplicate triples, so
    matching on name alone would hand one player's projection to another."""
    pool = _pool(("Palmer", "MID", "CHE"), ("Palmer", "DEF", "AVL"))
    row = _data_row(A="Palmer", B="AVL", C="DEF")
    rows, _ = ip.parse_rows(_cells_from([_header_row(), row]))
    matched, _, ambiguous = ip.resolve(rows, ip.build_index(pool))
    assert not ambiguous
    assert matched[0]["player_id"] == str(pool[1]["id"])


def test_an_ambiguous_row_is_never_guessed():
    pool = _pool(("Palmer", "MID", "CHE"), ("Palmer", "MID", "CHE"))
    rows, _ = ip.parse_rows(_cells_from([_header_row(),
                                                    _data_row(A="Palmer", B="CHE",
                                                              C="MID")]))
    matched, _, ambiguous = ip.resolve(rows, ip.build_index(pool))
    assert not matched and len(ambiguous) == 1


def test_an_unmatched_row_reports_its_near_misses():
    """The near miss is what turns an unmatched row into a one-line alias entry."""
    pool = _pool(("Haaland", "FWD", "LIV"))   # right player, wrong club
    rows, _ = ip.parse_rows(_cells_from([_header_row(), _data_row()]))
    matched, unmatched, _ = ip.resolve(rows, ip.build_index(pool))
    assert not matched and len(unmatched) == 1
    assert [p["name"] for p in unmatched[0]["near"]] == ["Haaland"]


def test_an_alias_pins_a_row_to_a_specific_player():
    pool = _pool(("Haaland", "FWD", "LIV"))
    rows, _ = ip.parse_rows(_cells_from([_header_row(), _data_row()]))
    matched, unmatched, _ = ip.resolve(rows, ip.build_index(pool),
                                       {"Haaland": str(pool[0]["id"])})
    assert not unmatched and matched[0]["player_id"] == str(pool[0]["id"])


# ---- storage (DB) ---------------------------------------------------------
def _player(session, name, code, fpl_id, team="MCI", pos="FWD"):
    p = Player(name=name, code=code, fpl_id=fpl_id, current_team=team, position=pos,
               price=50, status="a")
    session.add(p)
    session.flush()
    return p


def _proj(session, player, year=2026, **over):
    vals = {"raw_name": player.name, "raw_team": player.current_team,
            "raw_position": player.position, "price": 15.5, "points": 231.0}
    vals.update(over)
    pr = PlayerProjection(season_year=year, player_id=player.id, **vals)
    session.add(pr)
    session.flush()
    return pr


def test_a_projection_survives_an_fpl_id_reassignment(test_session):
    """FPL recycles element ids every season. Keying on fpl_id would hand Gabriel's
    projection to J.Timber at the next rollover — the incident CLAUDE.md documents,
    one table over."""
    a = _player(test_session, "Gabriel", 111, 5)
    b = _player(test_session, "J.Timber", 222, 6)
    _proj(test_session, a, points=180.0)
    a.fpl_id = None
    test_session.flush()
    b.fpl_id = 5                     # b now holds the element id a used to have
    test_session.commit()

    idx = services.projection_index(test_session, 2026)
    assert set(idx) == {a.id}, "the projection followed the element id, not the player"
    assert idx[a.id].points == 180.0


def test_one_projection_per_player_per_season(test_session):
    """Enforced by the MIGRATION, not just the model — conftest builds the schema with
    `alembic upgrade head`, so this is the only guard against the two diverging, which
    matters unusually much while autogenerate is banned in this repo."""
    p = _player(test_session, "Gabriel", 111, 5)
    _proj(test_session, p)
    test_session.commit()
    with pytest.raises(IntegrityError):
        _proj(test_session, p)
        test_session.commit()
    test_session.rollback()


def test_projection_index_is_keyed_on_players_id(test_session):
    """Not the projection row's own .id, which would compare False against every
    roster and keeper FK."""
    p = _player(test_session, "Gabriel", 111, 5)
    pr = _proj(test_session, p)
    test_session.commit()
    idx = services.projection_index(test_session, 2026)
    assert set(idx) == {p.id} and p.id != pr.id


def test_projection_index_is_scoped_to_one_season(test_session):
    p = _player(test_session, "Gabriel", 111, 5)
    _proj(test_session, p, year=2025, points=1.0)
    _proj(test_session, p, year=2026, points=231.0)
    test_session.commit()
    assert services.projection_index(test_session, 2026)[p.id].points == 231.0


# ---- which season we project ----------------------------------------------
def _league(session, fpl_id, year, *, current=True, locked=False, phase="offseason"):
    lg = League(fpl_league_id=fpl_id, name=f"S{year}", season_year=year,
                is_current=current, sync_locked=locked, phase=phase)
    session.add(lg)
    session.flush()
    return lg


def test_projection_season_is_none_before_any_import(test_session):
    """How the page knows to hide the whole group instead of rendering ten columns of
    em-dashes for every player."""
    assert services.projection_season_year(test_session) is None


def test_projection_season_is_the_newest_imported(test_session):
    p = _player(test_session, "Gabriel", 111, 5)
    _proj(test_session, p, year=2025)
    _proj(test_session, p, year=2026)
    test_session.commit()
    assert services.projection_season_year(test_session) == 2026


def test_projection_season_does_not_track_the_current_league_year(test_session):
    """`league.season_year + 1` is right only between GW38 and the rollover. Once
    advance_season flips is_current, +1 points past the imported data and every
    projected column silently blanks for a whole season."""
    _league(test_session, "1", 2026, current=True)
    p = _player(test_session, "Gabriel", 111, 5)
    _proj(test_session, p, year=2026)
    test_session.commit()
    assert services.projection_season_year(test_session) == 2026


def test_year_label_reads_a_bare_year(test_session):
    assert services.year_label(2026) == "26/27"
    assert services.year_label(None) == "00/01"


# ---- upsert ---------------------------------------------------------------
def _resolved(player, **over):
    row = {"raw_name": player.name, "raw_team": player.current_team,
           "raw_position": player.position, "price": 15.5, "minutes": 3120.0,
           "goals_scored": 27.5, "assists": 5.8, "clean_sheets": 0.0,
           "bonus": 34.7, "defensive_contributions": 0.4, "yellow_cards": 3.2,
           "points": 231.0, "player_id": str(player.id)}
    row.update(over)
    return row


def test_reimport_updates_in_place(test_session):
    """A revised sheet must overwrite, not duplicate — that is what the
    (season_year, player_id) key buys."""
    p = _player(test_session, "Haaland", 111, 5)
    test_session.commit()

    first = ip.upsert_projections(test_session, 2026, [_resolved(p)])
    test_session.commit()
    assert first["insert"] == 1

    again = ip.upsert_projections(test_session, 2026, [_resolved(p)])
    test_session.commit()
    assert (again["insert"], again["update"], again["identical"]) == (0, 0, 1)

    revised = ip.upsert_projections(test_session, 2026, [_resolved(p, points=250.0)])
    test_session.commit()
    assert revised["update"] == 1
    assert test_session.query(PlayerProjection).count() == 1
    assert test_session.query(PlayerProjection).one().points == 250.0


def test_a_row_the_sheet_no_longer_has_is_reported_stale_not_deleted(test_session):
    """A truncated export must never silently empty the table; --prune is opt-in."""
    a = _player(test_session, "Haaland", 111, 5)
    b = _player(test_session, "Palmer", 222, 6, team="CHE", pos="MID")
    test_session.commit()
    ip.upsert_projections(test_session, 2026, [_resolved(a), _resolved(b)])
    test_session.commit()

    counts = ip.upsert_projections(test_session, 2026, [_resolved(a)])
    test_session.commit()
    assert counts["stale"] == 1
    assert test_session.query(PlayerProjection).count() == 2
