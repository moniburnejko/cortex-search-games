# Test Show All Reference Comparison

## Zakres i metodologia

- Zrodlo referencyjne: `/Users/moniburnejko/Downloads/setup_2026-02-10-1304.csv` (2631 rekordow).
- Wyniki Cortex Search: `output/test_show_all_requests.csv` (ostatni uruchomiony `test_show_all.py`).
- Referencja liczona deterministycznie po kolumnach strukturalnych (`TAGS/GENRES/CATEGORIES`, `SUPPORTED_LANGUAGES`, `RELEASE_YEAR`) oraz regex-boundary dla `exclude` (`multiplayer`).
- Przyklad gry oznacza rekord z referencji; to nie jest ranking relevance, tylko pewny match wedlug reguly.

## Podsumowanie

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

## Szczegoly per query

### 1. I'm in the mood for a good indie game.

- Regula referencyjna: wymagane: temat `indie` (tag/genre/category).
- Referencja (CSV): 1940 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`indie`, excluded_terms=`[]`, attribute_filters_relaxed=True.
- Roznica: -940 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 51.5%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 392190 | #SelfieTennis | 2016
  - 554920 | 0 Day | 2016
  - 389120 | 10 Minute Barbarian | 2016
  - 413480 | 101 Ways to Die | 2016
  - 529950 | 12 orbits | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 373480 | 1993 Space Machine | 2016
  - 534320 | 4Team | 2016
  - 526890 | A Day in the Woods | 2016

### 2. Show me action games.

- Regula referencyjna: wymagane: temat `action` (tag/genre/category).
- Referencja (CSV): 1294 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`action games`, excluded_terms=`[]`, attribute_filters_relaxed=False.
- Roznica: -294 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 77.3%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 554920 | 0 Day | 2016
  - 413480 | 101 Ways to Die | 2016
  - 529950 | 12 orbits | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 373480 | 1993 Space Machine | 2016
  - 534320 | 4Team | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 550810 | Abduction Bit | 2016
  - 440470 | Absence | 2016

### 3. Find adventure games I can dive into.

- Regula referencyjna: wymagane: temat `adventure` (tag/genre/category).
- Referencja (CSV): 1168 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`adventure games immersive story exploration`, excluded_terms=`[]`, attribute_filters_relaxed=False.
- Roznica: -168 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 85.6%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 554920 | 0 Day | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 462530 | 8i - Make VR Human | 2016
  - 281200 | A Boy and His Blob | 2016
  - 526890 | A Day in the Woods | 2016
  - 517680 | A dead world's dream | 2016
  - 503820 | A Detective's Novel | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 500320 | A Tale of Caos: Overture | 2016
  - 509340 | Abandoned Hospital VR | 2016
  - 547040 | Above - VR | 2016
  - 440470 | Absence | 2016

### 4. Give me casual games to relax with.

- Regula referencyjna: wymagane: temat `casual` (tag/genre/category).
- Referencja (CSV): 1044 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`casual relaxing games`, excluded_terms=`[]`, attribute_filters_relaxed=True.
- Roznica: -44 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 95.8%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 439260 | "BUTTS: The VR Experience" | 2016
  - 389120 | 10 Minute Barbarian | 2016
  - 413480 | 101 Ways to Die | 2016
  - 529950 | 12 orbits | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 492160 | 3D Pool | 2016
  - 534320 | 4Team | 2016
  - 564340 | 5-in-1 Pack - Monument Builders: Destination USA | 2016
  - 526890 | A Day in the Woods | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 550810 | Abduction Bit | 2016
  - 523570 | Across Flash | 2016

### 5. I only want single-player games.

- Regula referencyjna: wymagane: single-player (tag `singleplayer` lub category `single-player`) + wykluczenie `multiplayer`.
- Referencja (CSV): 2038 rekordow.
- Cortex Search: result_count=712, shown=712, rewritten_query=`single player`, excluded_terms=`["multiplayer"]`, attribute_filters_relaxed=False.
- Roznica: -1326 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 34.9%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 439260 | "BUTTS: The VR Experience" | 2016
  - 303720 | #KILLALLZOMBIES | 2016
  - 392190 | #SelfieTennis | 2016
  - 554920 | 0 Day | 2016
  - 389120 | 10 Minute Barbarian | 2016
  - 413480 | 101 Ways to Die | 2016
  - 514180 | 18 Wheels of Steel: Haulin’ | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 564340 | 5-in-1 Pack - Monument Builders: Destination USA | 2016
  - 446590 | 7 Mages | 2016

### 6. Can you show me 2D games?

- Regula referencyjna: wymagane: temat `2d` (tag/genre/category).
- Referencja (CSV): 540 rekordow.
- Cortex Search: result_count=540, shown=540, rewritten_query=`2D games`, excluded_terms=`[]`, attribute_filters_relaxed=False.
- Roznica: 0 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 100.0%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 529950 | 12 orbits | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 373480 | 1993 Space Machine | 2016
  - 281200 | A Boy and His Blob | 2016
  - 500320 | A Tale of Caos: Overture | 2016
  - 550810 | Abduction Bit | 2016
  - 366760 | Adorables | 2016
  - 314970 | Age of Conquest IV | 2016
  - 497580 | Agent Walker: Secret Journey | 2016
  - 487370 | Akin | 2016
  - 524850 | Alicemare | 2016
  - 391310 | Alien Attack | 2016

### 7. Find indie games, but exclude multiplayer.

- Regula referencyjna: wymagane: `indie` + wykluczenie `multiplayer`.
- Referencja (CSV): 1557 rekordow.
- Cortex Search: result_count=820, shown=820, rewritten_query=`indie games`, excluded_terms=`["multiplayer"]`, attribute_filters_relaxed=True.
- Roznica: -737 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 52.7%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 392190 | #SelfieTennis | 2016
  - 554920 | 0 Day | 2016
  - 389120 | 10 Minute Barbarian | 2016
  - 413480 | 101 Ways to Die | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 526890 | A Day in the Woods | 2016
  - 503820 | A Detective's Novel | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 500320 | A Tale of Caos: Overture | 2016

### 8. Show action games without multiplayer modes.

- Regula referencyjna: wymagane: `action` + wykluczenie `multiplayer`.
- Referencja (CSV): 918 rekordow.
- Cortex Search: result_count=735, shown=735, rewritten_query=`action single player`, excluded_terms=`["multiplayer"]`, attribute_filters_relaxed=True.
- Roznica: -183 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 80.1%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 554920 | 0 Day | 2016
  - 413480 | 101 Ways to Die | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 550810 | Abduction Bit | 2016
  - 440470 | Absence | 2016
  - 501180 | Acan's Call: Act 1 | 2016
  - 450500 | Ace of Seafood | 2016
  - 468170 | AcidPunk : Echoes of Doll City | 2016

### 9. I'm looking for adventure games, preferably without multiplayer.

- Regula referencyjna: wymagane: `adventure` + wykluczenie `multiplayer`.
- Referencja (CSV): 973 rekordow.
- Cortex Search: result_count=852, shown=852, rewritten_query=`adventure single player`, excluded_terms=`["multiplayer"]`, attribute_filters_relaxed=True.
- Roznica: -121 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 87.6%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 554920 | 0 Day | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 462530 | 8i - Make VR Human | 2016
  - 281200 | A Boy and His Blob | 2016
  - 526890 | A Day in the Woods | 2016
  - 517680 | A dead world's dream | 2016
  - 503820 | A Detective's Novel | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 500320 | A Tale of Caos: Overture | 2016
  - 509340 | Abandoned Hospital VR | 2016
  - 547040 | Above - VR | 2016
  - 440470 | Absence | 2016

### 10. Show me indie games with English support released between 2014 and 2016.

- Regula referencyjna: wymagane: `indie` + jezyk `english` + release_year 2014..2016.
- Referencja (CSV): 1939 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`indie games`, excluded_terms=`[]`, attribute_filters_relaxed=True.
- Roznica: -939 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 51.6%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 392190 | #SelfieTennis | 2016
  - 554920 | 0 Day | 2016
  - 389120 | 10 Minute Barbarian | 2016
  - 413480 | 101 Ways to Die | 2016
  - 529950 | 12 orbits | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 373480 | 1993 Space Machine | 2016
  - 534320 | 4Team | 2016
  - 526890 | A Day in the Woods | 2016

### 11. Find action games from 2014 to 2016.

- Regula referencyjna: wymagane: `action` + release_year 2014..2016.
- Referencja (CSV): 1294 rekordow.
- Cortex Search: result_count=1000, shown=1000, rewritten_query=`action games`, excluded_terms=`[]`, attribute_filters_relaxed=False.
- Roznica: -294 (Cortex - Referencja).
- Pokrycie wzgledem referencji: 77.3%.
- Przykladowe pasujace gry (APP_ID, NAME, RELEASE_YEAR):
  - 303720 | #KILLALLZOMBIES | 2016
  - 554920 | 0 Day | 2016
  - 413480 | 101 Ways to Die | 2016
  - 529950 | 12 orbits | 2016
  - 470060 | 1917 - The Alien Invasion DX | 2016
  - 440810 | 1943 Megami Strike | 2016
  - 388320 | 1979 Revolution: Black Friday | 2016
  - 373480 | 1993 Space Machine | 2016
  - 534320 | 4Team | 2016
  - 383020 | a Family of Grave Diggers | 2016
  - 550810 | Abduction Bit | 2016
  - 440470 | Absence | 2016

## Uwagi interpretacyjne

- `result_count` z Cortex Search jest po rankingu hybrydowym i po odcieciu przez `min_score=0.5`; nie jest to pelna liczba rekordow spelniajacych warunki logiczne.
- Dla zapytan z bardzo szeroka intencja (`indie`, `action`, `adventure`, `casual`) Cortex czesto dochodzi do limitu 1000, podczas gdy referencja logiczna jest wieksza.
- Zapytania z `exclude multiplayer` maja duzy spadek `result_count` vs referencja, bo dodatkowo dziala ranking + prog score.
