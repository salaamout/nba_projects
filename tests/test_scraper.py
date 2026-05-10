"""Tests for scraper.py helper functions.

All network calls are mocked so these tests run fully offline.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import scraper
from scraper import (
    RateLimitError,
    _safe_float,
    _parse_table,
    _nba_season_str,
    abbr_to_team_name,
    abbr_to_team_name_variants,
    _to_nba_abbr,
    _get,
    _normalize_bbref_round,
    _hist_abbr_for_static,
)


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

class TestSafeFloat(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_safe_float(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_safe_float(""))

    def test_em_dash_returns_none(self):
        self.assertIsNone(_safe_float("—"))

    def test_plain_hyphen_returns_none(self):
        self.assertIsNone(_safe_float("-"))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(_safe_float("   "))

    def test_valid_float(self):
        self.assertAlmostEqual(_safe_float("3.14"), 3.14)

    def test_valid_integer_string(self):
        self.assertEqual(_safe_float("42"), 42.0)

    def test_non_numeric_returns_none(self):
        self.assertIsNone(_safe_float("abc"))


# ---------------------------------------------------------------------------
# _parse_table
# ---------------------------------------------------------------------------

_MINIMAL_HTML = """
<html><body>
<table id="stats">
  <thead>
    <tr>
      <th data-stat="ranker">Rk</th>
      <th data-stat="player">Player</th>
      <th data-stat="pts">PTS</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td data-stat="ranker">1</td>
      <td data-stat="player"><a href="/players/j/jordami01.html">Michael Jordan</a></td>
      <td data-stat="pts">30.1</td>
    </tr>
    <tr>
      <td data-stat="ranker">2</td>
      <td data-stat="player"><a href="/players/b/birdbla01.html">Larry Bird</a></td>
      <td data-stat="pts">24.3</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

_HEADER_ROW_HTML = """
<html><body>
<table id="stats">
  <thead>
    <tr>
      <th data-stat="ranker">Rk</th>
      <th data-stat="player">Player</th>
      <th data-stat="pts">PTS</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td data-stat="ranker">Rk</td>
      <td data-stat="player">Player</td>
      <td data-stat="pts">PTS</td>
    </tr>
    <tr>
      <td data-stat="ranker">1</td>
      <td data-stat="player">Michael Jordan</td>
      <td data-stat="pts">30.1</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

_COMMENTED_TABLE_HTML = """
<html><body>
<!--
<table id="hidden_stats">
  <thead>
    <tr>
      <th data-stat="ranker">Rk</th>
      <th data-stat="player">Player</th>
      <th data-stat="pts">PTS</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td data-stat="ranker">1</td>
      <td data-stat="player">LeBron James</td>
      <td data-stat="pts">27.0</td>
    </tr>
  </tbody>
</table>
-->
</body></html>
"""


class TestParseTable(unittest.TestCase):
    def test_basic_parse(self):
        soup = BeautifulSoup(_MINIMAL_HTML, "html.parser")
        rows = _parse_table(soup, "stats")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["player"], "Michael Jordan")
        self.assertEqual(rows[0]["pts"], "30.1")
        self.assertEqual(rows[1]["player"], "Larry Bird")

    def test_captures_player_href(self):
        soup = BeautifulSoup(_MINIMAL_HTML, "html.parser")
        rows = _parse_table(soup, "stats")
        self.assertIn("player_href", rows[0])
        self.assertIn("/players/", rows[0]["player_href"])

    def test_skips_header_rows(self):
        soup = BeautifulSoup(_HEADER_ROW_HTML, "html.parser")
        rows = _parse_table(soup, "stats")
        # Only 1 real data row; the mid-table header row must be filtered out
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player"], "Michael Jordan")

    def test_uncomments_tables(self):
        soup = BeautifulSoup(_COMMENTED_TABLE_HTML, "html.parser")
        rows = _parse_table(soup, "hidden_stats")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["player"], "LeBron James")

    def test_missing_table_raises(self):
        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        with self.assertRaises(ValueError):
            _parse_table(soup, "nonexistent")


# ---------------------------------------------------------------------------
# _get — 429 retry / RateLimitError
# ---------------------------------------------------------------------------

class TestGet429Retry(unittest.TestCase):
    def _make_429_response(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.raise_for_status = MagicMock()
        return resp

    @patch("scraper.time.sleep")
    @patch("scraper.requests.get")
    def test_raises_rate_limit_error_after_exhausted_retries(self, mock_get, mock_sleep):
        mock_get.return_value = self._make_429_response()
        with self.assertRaises(RateLimitError):
            _get("https://www.baseball-reference.com/fake", max_retries=3)
        # Should have slept between retries
        self.assertEqual(mock_sleep.call_count, 3)

    @patch("scraper.time.sleep")
    @patch("scraper.requests.get")
    def test_succeeds_after_one_429(self, mock_get, mock_sleep):
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.content = b"<html><body></body></html>"
        mock_get.side_effect = [self._make_429_response(), ok_resp]
        result = _get("https://www.basketball-reference.com/fake", max_retries=3)
        self.assertIsInstance(result, BeautifulSoup)
        self.assertEqual(mock_sleep.call_count, 1)


# ---------------------------------------------------------------------------
# _nba_season_str
# ---------------------------------------------------------------------------

class TestNbaSeasonStr(unittest.TestCase):
    def test_1978(self):
        self.assertEqual(_nba_season_str(1978), "1977-78")

    def test_2000(self):
        self.assertEqual(_nba_season_str(2000), "1999-00")

    def test_2024(self):
        self.assertEqual(_nba_season_str(2024), "2023-24")

    def test_1990(self):
        self.assertEqual(_nba_season_str(1990), "1989-90")


# ---------------------------------------------------------------------------
# abbr_to_team_name
# ---------------------------------------------------------------------------

class TestAbbrToTeamName(unittest.TestCase):
    def test_simple_abbr_no_year(self):
        self.assertEqual(abbr_to_team_name("LAL"), "Los Angeles Lakers")

    def test_cha_1999_is_hornets(self):
        self.assertEqual(abbr_to_team_name("CHA", 1999), "Charlotte Hornets")

    def test_cha_2020_is_hornets(self):
        self.assertEqual(abbr_to_team_name("CHA", 2020), "Charlotte Hornets")

    def test_unknown_abbr_returns_none(self):
        self.assertIsNone(abbr_to_team_name("ZZZ"))

    def test_case_insensitive(self):
        self.assertEqual(abbr_to_team_name("lal"), "Los Angeles Lakers")


# ---------------------------------------------------------------------------
# _to_nba_abbr  (bbref_to_nba_abbr)
# ---------------------------------------------------------------------------

class TestBbrefToNbaAbbr(unittest.TestCase):
    def test_pho_becomes_phx(self):
        self.assertEqual(_to_nba_abbr("PHO"), "PHX")

    def test_unknown_passes_through(self):
        self.assertEqual(_to_nba_abbr("LAL"), "LAL")

    def test_wsb_becomes_was(self):
        self.assertEqual(_to_nba_abbr("WSB"), "WAS")


# ---------------------------------------------------------------------------
# _normalize_bbref_round
# ---------------------------------------------------------------------------

class TestNormalizeBbrefRound(unittest.TestCase):
    def test_nba_finals(self):
        self.assertEqual(_normalize_bbref_round("NBA Finals"), "NBA Finals")

    def test_finals_alone_maps_to_nba_finals(self):
        self.assertEqual(_normalize_bbref_round("Finals"), "NBA Finals")

    def test_conference_finals(self):
        self.assertEqual(_normalize_bbref_round("Conference Finals"), "Conference Finals")

    def test_division_finals_maps_to_conference_finals(self):
        self.assertEqual(_normalize_bbref_round("Division Finals"), "Conference Finals")

    def test_conference_semifinals(self):
        self.assertEqual(_normalize_bbref_round("Conference Semifinals"), "Conference Semifinals")

    def test_division_semifinals_maps_to_conference_semifinals(self):
        self.assertEqual(_normalize_bbref_round("Division Semifinals"), "Conference Semifinals")

    def test_first_round(self):
        self.assertEqual(_normalize_bbref_round("First Round"), "First Round")

    def test_quarterfinals_maps_to_first_round(self):
        self.assertEqual(_normalize_bbref_round("Quarterfinals"), "First Round")

    def test_strips_asterisk(self):
        self.assertEqual(_normalize_bbref_round("NBA Finals*"), "NBA Finals")

    def test_case_insensitive(self):
        self.assertEqual(_normalize_bbref_round("DIVISION FINALS"), "Conference Finals")


# ---------------------------------------------------------------------------
# _hist_abbr_for_static
# ---------------------------------------------------------------------------

class TestHistAbbrForStatic(unittest.TestCase):
    def test_bkn_2002_returns_njn(self):
        self.assertEqual(_hist_abbr_for_static("BKN", 2002), "NJN")

    def test_bkn_2013_returns_bkn(self):
        # 2013 is after override window ends, so no override → pass-through
        self.assertEqual(_hist_abbr_for_static("BKN", 2013), "BKN")

    def test_nop_2008_returns_noh(self):
        self.assertEqual(_hist_abbr_for_static("NOP", 2008), "NOH")

    def test_cha_2002_returns_chh(self):
        self.assertEqual(_hist_abbr_for_static("CHA", 2002), "CHH")

    def test_cha_2014_returns_cha(self):
        # 2014 is past the CHH window → no override
        self.assertEqual(_hist_abbr_for_static("CHA", 2014), "CHA")

    def test_no_override_passes_through(self):
        self.assertEqual(_hist_abbr_for_static("LAL", 2005), "LAL")


if __name__ == "__main__":
    unittest.main()
