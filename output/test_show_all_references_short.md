# Test Show All Reference - Short

Skrot porownania referencji z `setup_2026-02-10-1304.csv` vs wyniki Cortex z `output/test_show_all_requests.csv`.

| # | Query | Regula referencyjna | Referencja | Cortex result_count | Delta (Cortex-Ref) | Pokrycie |
|---:|---|---|---:|---:|---:|---:|
| 1 | I'm in the mood for a good indie game. | wymagane: temat `indie` (tag/genre/category) | 1940 | 1000 | -940 | 51.5% |
| 2 | Show me action games. | wymagane: temat `action` (tag/genre/category) | 1294 | 1000 | -294 | 77.3% |
| 3 | Find adventure games I can dive into. | wymagane: temat `adventure` (tag/genre/category) | 1168 | 1000 | -168 | 85.6% |
| 4 | Give me casual games to relax with. | wymagane: temat `casual` (tag/genre/category) | 1044 | 1000 | -44 | 95.8% |
| 5 | I only want single-player games. | wymagane: single-player (tag `singleplayer` lub category `single-player`) + wykluczenie `multiplayer` | 2038 | 712 | -1326 | 34.9% |
| 6 | Can you show me 2D games? | wymagane: temat `2d` (tag/genre/category) | 540 | 540 | 0 | 100.0% |
| 7 | Find indie games, but exclude multiplayer. | wymagane: `indie` + wykluczenie `multiplayer` | 1557 | 820 | -737 | 52.7% |
| 8 | Show action games without multiplayer modes. | wymagane: `action` + wykluczenie `multiplayer` | 918 | 735 | -183 | 80.1% |
| 9 | I'm looking for adventure games, preferably without multiplayer. | wymagane: `adventure` + wykluczenie `multiplayer` | 973 | 852 | -121 | 87.6% |
| 10 | Show me indie games with English support released between 2014 and 2016. | wymagane: `indie` + jezyk `english` + release_year 2014..2016 | 1939 | 1000 | -939 | 51.6% |
| 11 | Find action games from 2014 to 2016. | wymagane: `action` + release_year 2014..2016 | 1294 | 1000 | -294 | 77.3% |

## Kluczowe wnioski

- Liczba zapytan: 11.
- Zapytania dobite do limitu 1000 (`result_count=1000`): 6/11.
- Najlepsze pokrycie: 100.0% (`Can you show me 2D games?`).
- Najslabsze pokrycie: 34.9% (`I only want single-player games.`).
- Szerokie zapytania i zapytania z wykluczeniami czesto traca recall przez ranking hybrydowy + prog `min_score=0.5` + limit 1000.

Pelna analiza: `output/test_show_all_references.md`
