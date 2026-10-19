# Konfiguracja i wdrożenie

[English / pełny opis operacyjny](setup.md)

Wymagane są Python 3.12+, konto Librus, uwierzytelniony Codex CLI z dostępnymi modelami oraz połączony Google Calendar z zapisem do wskazanego kalendarza. Modele i identyfikator konektora zależą od konta. Przykłady są fikcyjne.

Domyślnie analiza oraz wszystkie wywołania narzędzi kalendarza — sprawdzanie dostępu, odczyty, uzgadnianie wyników i zapisy — używają `gpt-6.1-sol` z rozumowaniem `high`. Potwierdź ich dostępność w swoim środowisku. Jawne prywatne `tool_model` i `tool_effort` mają pierwszeństwo; starszą konfigurację zmień świadomie, jeśli chcesz przyjąć nową wartość domyślną.

Prywatne `config.json`, `credentials.json`, `web.env` i `monitor.env` przechowuj w `$HOME/.config/librus-calendar`, w katalogu 0700 i plikach 0600. Baza pozostaje w `$HOME/.local/share/librus-calendar`. `deploy/config.json.example` dokumentuje ustawienia. Ustaw dokładny kalendarz, właściciela, konektor, właściciela Tailscale i dozwolony host; produkcyjne `allow_local` ma pozostać `false`. Automatyczne kopie bazy są celowo wyłączone przez `backup_enabled=false`. Nigdy nie zapisuj haseł w argumentach poleceń ani repozytorium.

Kod produkcyjny jest instalowany jako pakiet w `$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv`. Dowiązanie `current` wybiera wersję. Checkout do rozwoju jest osobny; Każde polecenie uruchamiane Pythonem pakietu, także ręczne status/sync i administracja, musi zawierać `-I`: katalog roboczy, odziedziczony PYTHONPATH i user site nie mogą zastąpić kodu wdrożenia. Jednostki systemd wymuszają `-I`.

```sh
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python scripts/deploy.py prepare --sha FULL_SHA
```

`FULL_SHA` oznacza pełny, sprawdzony SHA zgodny z HEAD czystego checkoutu i osiągalny z lokalnego, wcześniej pobranego `origin/main`. Przygotowanie sprawdza prywatność źródeł i historii, uruchamia testy offline w izolowanym katalogu domowym, buduje pakiet, instaluje zależności i sprawdza zasoby poza checkoutem. Manifest wiąże SHA z plikami wdrożenia i zainstalowaną aplikacją. Nie zatrzymuje usług, nie czyta bazy produkcyjnej i nie robi jej kopii.

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA
.venv/bin/python scripts/deploy.py verify
```

Aktywacja najpierw zatrzymuje timer nowych synchronizacji. Jeśli pracuje proces zapisujący kalendarz, odmawia przełączenia; nigdy go nie zabija. Spróbuj ponownie po zakończeniu. Wszystkie kontrole odbywają się przed zmianami. Odmowa obejmuje inne istniejące jednostki, dowiązania i nakładki systemd. Instalator zachowuje istniejące ustawienia prywatne.

Pierwsza, jawnie zatwierdzona migracja znanych starych jednostek może użyć:

```sh
.venv/bin/python scripts/deploy.py activate --sha FULL_SHA --migrate-legacy --no-sync
.venv/bin/python scripts/deploy.py verify --no-sync
```

`--no-sync` uruchamia WWW i monitoring, pozostawiając synchronizację wyłączoną do odbioru pierwszego przebiegu. Po odbiorze włącz `librus-sync.timer` i wykonaj zwykłe `verify`. Tylko ta jawna migracja usuwa stare jednostki backupu. Instalator i wdrożenie nie kopiują SQLite. `bash scripts/install_linux.sh --sha FULL_SHA` instaluje same jednostki bez startu usług.

WWW nasłuchuje wyłącznie na `127.0.0.1:8795`; zachowaj prywatną trasę Tailscale Serve HTTPS 8445 i wszystkie inne trasy. Nigdy nie włączaj Funnel. Timer synchronizacji działa co pełną godzinę w Europe/Warsaw i ogranicza cały przebieg do 45 minut. Monitoring działa co minutę.

Instalator root `deploy/install-librus-monitor.sh APP_USER` kopiuje wyłącznie kontrolowane pliki Netdata. `--migrate-legacy` dopuszcza tylko znane skróty poprzednich plików; późniejsze aktualizacje korzystają z rejestru skrótów należącego do root. Instalator nie restartuje Netdata. Zmiana kolektora Python wymaga kontrolowanego restartu odpowiedniego procesu, a nie tylko przeładowania reguł zdrowia.

`monitor.env` musi zawierać pusty `LIBRUS_CONFIG_FILE=`, `LIBRUS_BACKUP_ENABLED=0` oraz ten sam `LIBRUS_STATE_PATH`, właściciela i host co konfiguracja WWW. Systemd nie rozwija `$HOME` w tym pliku: użyj prywatnej ścieżki bezwzględnej albo `~`. Monitoring nie ma dostępu do prywatnego katalogu konfiguracji. Eksportuje tylko zbiorcze, odczytane bez zmian czasy, liczniki i stany. Celowo wyłączony backup nie zgłasza alarmów; błędy synchronizacji, WWW, bazy i świeżości kolektora nadal są widoczne. Wyciszenie konserwacyjne ma być ograniczone czasowo i przywracać dokładnie wcześniejsze reguły.

Schemat zbiorczy w wersji 1 rozszerzają opcjonalne pola dopuszczające null: `calendar_probe_result`, `calendar_probe_attempts`, `calendar_probe_recovered`, `calendar_probe_retries_24h`, `calendar_probe_recoveries_24h`. Starsze eksportery/kolektory mogą pominąć nowe pola, a nowy kolektor przyjmuje stary format. Brak pola lub null oznacza **brak danych**, nie sukces ani zero ponowień. Informacyjny wykres `librus_local.calendar_probe` pokazuje bezwzględne liczniki rozpoczętych ponowień i skutecznych odzyskań w ruchomym oknie 24 godzin. Nie dodaje alarmów; skuteczna druga próba nie zwiększa licznika błędów synchronizacji. Aby zobaczyć wykres, zaktualizuj kolektor i zrestartuj jego proces.

## Sprawdzanie kalendarza i bezpieczna diagnostyka

Przed zwykłą synchronizacją rzeczywisty odczyt listy kalendarzy potwierdza prawo właściciela/zapisu do dokładnie wskazanego kalendarza. Wykonywane są najwyżej dwie próby, z pięcioma sekundami przed drugą. Ponowienie dopuszcza wyłącznie strukturalny błąd nieoczekiwanej liczby zakończonych wywołań (zero lub wiele), gdy wszystkie zaobserwowane wywołania są oczekiwanym odczytem `list_calendars` z właściwymi argumentami. Niezakończone wywołania, inne wywołania/argumenty, mutacje, błędny protokół/wynik, timeout, brak uprawnień lub brak kalendarza/prawa zapisu nie kwalifikują się. Druga próba ma świeży stan wywołania.

Panel pokazuje wynik próby ostatniego przebiegu synchronizacji (`running`, `ok`, `failed`, `interrupted`), liczbę prób i odzyskanie. Przebieg bez zapisanej próby oraz stara baza bez dowodów pokazują „Brak danych”. Skuteczna druga próba pokazuje potwierdzony dostęp, dwie próby i odzyskanie bez ostrzeżenia. Potwierdzenie odczytu dotyczy dostępu w danej chwili; osobno sprawdź końcowy wynik synchronizacji i dziennik operacji.

Historia prób przechowuje wyłącznie techniczne metadane: identyfikator przebiegu/próby, czasy, skonfigurowany model/poziom, zweryfikowaną wersję CLI, wynik, kod procesu/dozwolony kod błędu, liczniki wywołań/protokołu i flagi zgodności oczekiwanego wywołania. Nie zawiera ID kalendarzy, argumentów/wyników narzędzi, wiadomości, promptów, tokenów ani surowych błędów i wygasa po 90 dniach. Monitoring eksportuje tylko pięć pól zbiorczych. Zacznij od lokalnego statusu zainstalowanego CLI, panelu, stanu usług i oczyszczonych metadanych. Udostępniaj tylko kategorie wyniku/błędu i liczniki; nie publikuj zrzutu bazy ani transkryptów Codex. Samodzielna próba konfiguracji czyta kalendarz, ale nie tworzy dowodów przebiegu synchronizacji.

Zasada ponowienia dotyczy tylko tego początkowego odczytu. Nie ponawia bezpośrednio tworzenia, aktualizacji ani usuwania. Niepewny zapis wymaga zachowania dziennika i sprawdzenia wyniku odczytem przed późniejszą decyzją o wykonaniu pracy.

## Obsługa jednej agendy

Zwykły `sync` pobiera wymagane źródła i aktualny stan własnych wpisów Calendar. Błąd pobrania nie oznacza pustej listy. Wybrany uczeń i klasa muszą pozostać zweryfikowane. Jeden planer `gpt-6.1-sol/high` zastępuje dawny łańcuch ekstrakcji, dopasowania i dodatkowej weryfikacji. `multi_source_mode` jest nieaktywnym ustawieniem zgodności.

```sh
release_python="$HOME/.local/lib/librus-calendar/current/.venv/bin/python"
"$release_python" -I -m app.cli status
"$release_python" -I -m app.cli agenda-preview
```

`agenda-preview` i `sync --dry-run` korzystają z ostatniego pełnego udanego zbioru źródeł w bazie oraz świeżego odczytu Calendar. Nie logują się do Librusa i nie odświeżają źródeł. Brak pełnego zbioru daje widoczny błąd. Zapisują tylko izolowany podgląd; zero zmian aktywnych decyzji, grup, kolejki i Calendar. Tekst trafia do skonfigurowanej usługi planowania. `/preview` pokazuje czas zebrania źródeł; zapisany wynik nie potwierdza obecnej aktualności. Zwykła synchronizacja sprawdza źródła, agendę, kalendarz i decyzje przed ewentualnym użyciem identycznego planu. Zwykły wynik CLI zawiera tylko liczniki i kody.

Istniejące API wiadomości, propozycji, grup i źródeł pozostaje zgodne. `/api/actions/{id}` pokazuje jedno działanie ze źródłami. `/api/agenda` zwraca osobno metadane najnowszego planu i podglądu oraz zapisany podgląd całej agendy. `/api/source-previews` to jawnie oznaczona historia dawnego planera, bez wpływu na gotowość nowego. Proces WWW nie uruchamia inference ani aktywacji.

Aktualizuj w miejscu, z tą samą ścieżką bazy. Zatrzymaj nowe przebiegi godzinowe, pozwól writerowi skończyć i rozlicz unknown. Wykonaj kanoniczne `prepare` → `activate` → `verify`, zachowując konfigurację, stan wstrzymania i dzienniki. Inicjalizacja dołącza obecne wydarzenia/propozycje/działania idempotentnie: bez resetu i tworzenia wpisów Calendar. Nie zakładaj nowej bazy, nie kopiuj ani nie przywracaj SQLite, nie przeliczaj całej historii jako nowych spraw i nie używaj dawnych poleceń aktywacji źródeł ani reanalizy wiadomości. Po sprawdzeniu zachowanych ID i decyzji, braku duplikatów/unknown oraz pełnego pobrania przywróć ten sam timer godzinowy. Bez dodatkowej usługi i automatyzacji akceptacyjnej Codex.

## Retencja i naprawa

`prune --days 30` domyślnie pokazuje podgląd; `--apply` usuwa przedawnione treści źródeł, planów i audytów w miejscu. Pozostają minimalne ID, skróty, decyzje, własność, przyszłe fakty, tombstones i każdy wiersz dziennika. Pełne intencje unknown/inflight pozostają do rozstrzygnięcia. Techniczna historia synchronizacji/prób ma osobne 90 dni retencji. Pruning nie zapisuje Calendar i nie tworzy backupu.

Naprawiaj sprawdzoną nową wersją, zachowując bieżącą bazę i dziennik. Powrót do starszego kodu wymaga potwierdzonej zgodności schematu i operacji. Nie przywracaj starszej bazy: utrata dowodów zapisów grozi duplikatami. Kody `agenda_*` i błędy źródeł są widoczne; przepełnienie, wadliwy lub nieaktualny plan blokują zapis zamiast pomijać dane. Monitoring odczytuje wyłącznie zbiorcze techniczne wyniki planów i liczniki źródeł; wykresy i progi alarmów pozostają dotychczasowe.

Demo `scripts/ui_fixture.py --port 8796` używa wyłącznie fikcyjnych danych i tymczasowej bazy. Prawdziwe źródła, konfiguracja, snapshoty i wyniki pozostają poza Git.
