# Konfiguracja i wdrożenie

[English / pełny opis operacyjny](setup.md)

Wymagane są Python 3.12+, konto Librus, uwierzytelniony Codex CLI z dostępnymi modelami oraz połączony Google Calendar z zapisem do wskazanego kalendarza. Modele i identyfikator konektora zależą od konta. Przykłady są fikcyjne.

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

Demo `scripts/ui_fixture.py --port 8796` korzysta wyłącznie z fikcyjnych danych i tymczasowej bazy. Ocenę prawdziwych wiadomości wykonuj na prywatnym pliku poza Git:

```sh
.venv/bin/python -m scripts.evaluate_analysis --config PRIVATE_EVAL_CONFIG PRIVATE_CASES_JSON
```

Ocenianie nie odkrywa konfiguracji produkcyjnej, nie otwiera bazy i wymusza runtime tylko do odczytu. Treść trafia do skonfigurowanej usługi AI. Pozostałe narzędzia rozwojowe muszą mieć odrębny stan i port, bez dostępu do produkcyjnej bazy i zapisu w kalendarzu.

Naprawiaj przez nową, sprawdzoną wersję kodu i zachowanie bieżącej bazy wraz z dziennikiem operacji. Powrót do poprzedniego pakietu jest możliwy tylko przy zgodności ze schematem i dziennikiem: wybierz czysty checkout poprzedniego SHA i użyj `activate --sha PREVIOUS_FULL_SHA`. Nie przywracaj starego SQLite; utrata dowodów wykonanych operacji może spowodować duplikaty w zewnętrznym kalendarzu. Najpierw rozstrzygnij niepewne wyniki, potem wznów zapisy.

Migrację wykonuj Pythonem przygotowanego pakietu przed aktywacją. Zatrzymaj stare nowe przebiegi i poczekaj na zakończenie pracującego procesu. Docelowa baza musi być inna niż źródłowa i mieć wstrzymane zapisy:

```sh
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" pause-writes
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" migrate-decisions --from "$HOME/.local/share/librus-calendar/state.sqlite3"
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" rebuild --days 30
"$release_python" -I -m app.cli --config "$HOME/.config/librus-calendar/config.json" --state "$HOME/.local/share/librus-calendar/current.sqlite3" prune --days 30
```

`migrate-decisions` odczytuje źródło bezpośrednio i tylko do odczytu; nie kopiuje bazy ani treści wiadomości. Zachowuje minimalne decyzje człowieka, dokładne ID wydarzeń, własność, tombstones anulowań i nierozstrzygnięte operacje. Tożsamość konta Librus i powiązany właściciel/kalendarz muszą być zgodne. Wyłącznie dla jawnie sprawdzonej starej bazy bez powiązania dodaj `--allow-legacy-binding`; wymaga to odczytowej kontroli dostępu właściciela w kalendarzu i zgodnego kalendarza każdego zapisanego wydarzenia.

`rebuild --days 30` przegląda wszystkie strony wiadomości z wybranego okresu, ponawia błędy pobierania/analizy oraz czyta odkryte i przeniesione ID wydarzeń. Wymusza kalendarz tylko do odczytu: nigdy nie kolejkuje ani nie wykonuje zapisów. Zmienia lokalny stan docelowy i przesyła wiadomości do analizy. Brakujące, obce lub niejednoznaczne własności wymagają przeglądu; wyniki partial/review nie oznaczają pełnej odbudowy. Dopuszczalny okres wynosi 1–30 dni.

`prune --days 30` pokazuje podgląd. Dopiero `--apply` usuwa stare treści wiadomości, audyt analizy i zbędne treści wydarzeń/operacji. Zachowuje minimalne ID, skróty, decyzje, własność, dowody konfliktów, tombstones oraz wszystkie wiersze dziennika. Pełne intencje unknown/inflight pozostają do rozstrzygnięcia; przedawnione niewykonane operacje przechodzą do przeglądu. Secure delete i VACUUM działają w miejscu, bez backupu. Pruning nie zmienia zewnętrznego kalendarza. Starą bazę usuń wyłącznie w jawnie określonym punkcie odbioru; skrypt wdrożenia jej nie usuwa. Szczegóły: [kontrakt aplikacji](../app/CONTRACT.md).
