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

Demo `scripts/ui_fixture.py --port 8796` korzysta wyłącznie z fikcyjnych danych i tymczasowej bazy. Ocenę prawdziwych wiadomości wykonuj na prywatnym pliku poza Git:

```sh
.venv/bin/python -m scripts.evaluate_analysis --config PRIVATE_EVAL_CONFIG PRIVATE_CASES_JSON
```

Ocenianie nie odkrywa konfiguracji produkcyjnej, nie otwiera bazy i wymusza runtime tylko do odczytu. Treść trafia do skonfigurowanej usługi AI. Pozostałe narzędzia rozwojowe muszą mieć odrębny stan i port, bez dostępu do produkcyjnej bazy i zapisu w kalendarzu.

Naprawiaj przez nową, sprawdzoną wersję kodu i zachowanie bieżącej bazy wraz z dziennikiem operacji. Powrót do poprzedniego pakietu jest możliwy tylko przy zgodności ze schematem i dziennikiem: wybierz czysty checkout poprzedniego SHA i użyj `activate --sha PREVIOUS_FULL_SHA`. Nie przywracaj starego SQLite; utrata dowodów wykonanych operacji może spowodować duplikaty w zewnętrznym kalendarzu. Najpierw rozstrzygnij niepewne wyniki, potem wznów zapisy.

Wynik kalendarza wymaga zweryfikowanych dowodów dokładnie dozwolonego narzędzia MCP i argumentów, niezależnie od transportu zwykłego MCP lub code-mode; sama deklaracja sukcesu modelu nie wystarcza.

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

`prune --days 30` pokazuje podgląd. Dopiero `--apply` usuwa stare treści wiadomości, audyt analizy i zbędne treści wydarzeń/operacji. Podglądy i audyty analizy źródeł zawierające opis podlegają 30 dniom retencji treści; 90-dniowa historia dotyczy wyłącznie technicznych przebiegów i prób Calendar. Zachowuje minimalne ID, skróty, decyzje, własność, dowody konfliktów, tombstones oraz wszystkie wiersze dziennika. Pełne intencje unknown/inflight pozostają do rozstrzygnięcia; przedawnione niewykonane operacje przechodzą do przeglądu. Secure delete i VACUUM działają w miejscu, bez backupu. Pruning nie zmienia zewnętrznego kalendarza. Starą bazę usuń wyłącznie w jawnie określonym punkcie odbioru; skrypt wdrożenia jej nie usuwa. Szczegóły: [kontrakt aplikacji](../app/CONTRACT.md).

## Wdrożenie wielu źródeł

`multi_source_mode` przyjmuje `off` (domyślnie), `preview` lub `active`; jawne nadpisanie środowiskowe to `LIBRUS_MULTI_SOURCE_MODE`. Dotychczasowa synchronizacja wiadomości pozostaje dostępna. Użyj tej samej chronionej lokalnej konfiguracji i procedury niezmiennego pakietu opisanej wyżej; identyfikatora ucznia, danych logowania, treści źródeł i wyników podglądu nie umieszczaj w publicznym repozytorium. Zachowaj powiązanie ucznia i kalendarza, uwierzytelnienie właściciela, stan wstrzymania zapisów, harmonogram godzinowy, model/effort i celowo wyłączone kopie bazy.

Pobieranie i analiza podglądu zaczynają się po dotychczasowej analizie wiadomości, uzgadnianiu wyników i zapisach Calendar. Ta osobna faza ma limit dziesięciu minut, połowy pozostałego czasu całej synchronizacji oraz 31 wywołań modelu łącznie na ekstrakcję, dopasowanie, odzyskiwanie starszych dowodów i niezależną weryfikację semantyczną. Dla małego źródła rezerwacja obejmuje jedną ekstrakcję i do trzech wywołań na działanie inne niż ignore (dopasowanie, odzyskanie i weryfikację): `1 + 3N <= 31`, czyli najwyżej dziesięć takich działań przy dostępnej funkcji odzyskiwania. Bez niej obowiązuje `1 + 2N <= 31`. Na działanie przypada najwyżej jedno grupowe odzyskiwanie dla maksymalnie 12 kandydatów; rezerwa zawsze chroni pozostałe podstawowe dopasowania. Źródło mieszczące się w świeżym budżecie jest odkładane w całości, gdy bieżący budżet nie wystarcza; niepełny wynik nie jest zapisywany jako gotowy. Wyjątkowo duże źródła rezerwują wywołania na podstawowe dopasowanie i mogą pokazać jawny niezweryfikowany wynik do sprawdzenia po wyczerpaniu budżetu opcjonalnej weryfikacji. Taki wynik nie ustala działania docelowego, powiązania, uprawnienia do zapisu ani potwierdzonej tożsamości działania. Gotowe podglądy pozostają zapisane; odłożone lub przerwane wersje źródeł zostają w trwałej kolejce na kolejne przebiegi, z widocznym powodem. Wyczerpanie budżetu podglądu nie zmienia sukcesu synchronizacji wiadomości. Fazę wykonuje główny wątek procesu CLI; wywołania z wątków tła ją odkładają. Pełny sukces trybu aktywnego nadal wymaga kompletnej obsługi wszystkich wymaganych kanałów.

1. Zweryfikuj wydanie testami fikcyjnych kolektorów, ponownego przetwarzania źródeł i działań, migracji i retencji, zachowania decyzji oraz UI. Sprawdź bezpieczne agregaty i dostępność źródeł. Sukces offline nie potwierdza zgodności z rzeczywistą szkołą.
2. Przygotuj sprawdzony niezmienny pakiet z nowymi źródłami w `preview`. Pobieranie musi potwierdzić wybranego uwierzytelnionego ucznia, wszystkie strony i pełne treści. Nieobsługiwane, częściowe lub niejednoznaczne pobranie pozostaje jawnie niepełne. Analizy podglądu są oddzielone od rzeczywistych propozycji, operacji i decyzji; załączniki pozostają metadanymi, bez żądań obrazów, OCR, PDF i treści załączników.
3. Zaobserwuj rzeczywisty godzinowy przebieg harmonogramu w podglądzie. Sprawdź pełne pobranie każdego wymaganego źródła, kolejkę, błędy, potwierdzenie ucznia i komplet stron. Prywatnie obejrzyj wyniki i niepewne duplikaty. Potwierdź zero zapisów Calendar z podglądu oraz brak zmian decyzji i identyfikatorów własnych wydarzeń. Ręczne uruchomienie ani przyszły termin timera nie spełniają tej bramki.
4. Po spełnieniu kryteriów podglądu i rzeczywistego przebiegu harmonogramu zmień wyłącznie zatwierdzony tryb na `active`. Aktywacja musi zawsze ponownie pobrać, przeanalizować i zweryfikować bieżącą treść, zakres ucznia, powiązania działań i decyzje rodzica; zapisany podgląd nie uprawnia do wykonania nieaktualnych działań.
5. Zaobserwuj rzeczywisty pełny godzinowy przebieg aktywny. Wymagaj pełnego pobrania aktywnych źródeł, braku automatycznej kolejki i nieznanych zapisów, niezmienionych decyzji i braku duplikatu przy pełnym powtórzeniu. Oddzielnie sprawdź UI i monitoring agregatów. Wdrożenie, podgląd, aktywację i pełny odbiór godzinowy opisuj osobno jako oczekujące albo zakończone.

Panel pokazuje wyniki dodano, powiązano, uzupełniono, do sprawdzenia, pominięto i analiza niepełna, z powodami i pochodzeniem. Kształty `/api/messages`, `/api/proposals` i `/api/proposal-groups` pozostają zgodne. `/api/sources?limit=50&offset=0&kind=announcement` zwraca `{items,limit,offset,next_offset}` (limit 1–100); rodzaje to `message`, `announcement`, `school_event`, `substitution` (`school_calendar` jest aliasem). `/api/actions/{id}` zawiera źródła, powiązania i zachowaną decyzję. `/api/source-previews` jest stronicowany i tylko do odczytu; nie ma endpointu aktywacji przez panel.

Netdata otrzymuje wyłącznie dozwolone agregaty dla rodzaju źródła: tryb, ostatnie pełne pobranie, liczbę oczekujących analiz i błędów oraz liczbę powiązań z działaniami, a także osobny licznik gotowych podglądów. Brak danych w starszej bazie lub eksporcie pozostaje nieznany. Świeżość podglądu nie przesuwa daty pełnego sukcesu aktywnej synchronizacji; każde wymagane aktywne źródło musi być kompletne. Progi alarmowe i trasy pozostają niezmienione, wraz z celowym wyłączeniem kopii bazy.

### Dokładne polecenia aktywacji i powrót po odmowie

Po rzeczywistym godzinowym odbiorze podglądu zatrzymaj wyłącznie timer i poczekaj na zakończenie aktywnego zapisu. Nie zatrzymuj ani nie zabijaj pracującego `librus-sync.service`. Zabezpiecz krótki czas planowania i wykonania przed decyzjami w panelu. Zachowaj zapisany stan wstrzymania zapisów. Oba polecenia administracyjne zajmują `sync.lock`; `already_running` oznacza oczekiwanie i ponowienie po zakończeniu bieżącego procesu.

```sh
systemctl --user stop librus-sync.timer
systemctl --user show librus-sync.service -p ActiveState -p SubState -p Result -p ExecMainStatus
release_python="$HOME/.local/lib/librus-calendar/releases/FULL_SHA/.venv/bin/python"
source_config="$HOME/.config/librus-calendar/config.json"
source_state="$HOME/.local/share/librus-calendar/current.sqlite3"
source_admin_dir="$HOME/.local/share/librus-calendar/admin"
install -d -m 0700 "$source_admin_dir"
activation_plan="$source_admin_dir/activation-$(date -u +%Y%m%dT%H%M%SZ).json"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" source-activate --plan-file "$activation_plan"
```

Ostatnie polecenie wykonaj dopiero przy nieaktywnej usłudze. Planowanie wymaga chronionego trybu `preview`, kompletnego zweryfikowanego pobrania trzech nowych kanałów i gotowego podglądu każdej bieżącej oczekującej wersji źródła. Tworzy nowy, nienadpisywany prywatny plan 0600 związany z wersjami źródeł, uczniem i kalendarzem, decyzjami, wydarzeniami i dziennikiem; wynik zawiera agregaty i `calendar_writes=0`. Plan zawiera prywatne identyfikatory; nie wyświetlaj go publicznie, nie zapisuj w Git ani nie udostępniaj.

Obejrzyj chroniony plan prywatnie. Atomowo zmień wyłącznie `multi_source_mode` w chronionej konfiguracji na `active`, zachowując właściciela, uprawnienia i pozostałe pola. Sprawdź również skuteczne nadpisania środowiskowe. Timer pozostaje zatrzymany. **Sama konfiguracja nie dostarcza brakującego potwierdzenia aktywacji.** Rozpoczęcie wykonania aktywacji trwale zawiesza starsze potwierdzenie do ukończenia nowej próby; po błędzie lub restarcie wspólna analiza, kolejka i zajmowanie operacji pozostają zablokowane. Prawidłowe wcześniejsze potwierdzenie zachowuje ważność przy zmianach trybu konfiguracji, gdy nie rozpoczęto nowej próby wykonania. Zapisy wymagają związanego potwierdzenia aktywacji oraz kompletnego pobrania wymaganych źródeł.

```sh
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" source-activate --plan-file "$activation_plan" --apply
```

Wykonanie ponownie pobiera źródła i porównuje ucznia oraz treść z planem, czyta dokładnie dozwolony kalendarz i sprawdza skróty własnych wydarzeń, a następnie ponownie analizuje i dopasowuje działania. Nie wykonuje zapisanych propozycji podglądu. Przed pobieraniem zapisuje trwały znacznik nieukończonej aktywacji. Wszystkie pola potwierdzenia aktywacji oraz usunięcie znacznika zapisują się atomowo dopiero po sukcesie wszystkich kroków; samo polecenie zgłasza zero zapisów Calendar. Częściowa lub przerwana próba pozostawia blokadę wspólnej analizy, kolejki i zajmowania operacji. Świeży plan ponowienia musi obejmować dokładny wybór źródeł z nieudanej próby i ponownie je analizować; wcześniejszy podgląd służy wyłącznie potwierdzeniu wyboru. Po sukcesie sprawdź zachowanie stanu i kolejki przed przywróceniem zapisanego stanu timera:

```sh
systemctl --user restart librus-web.service
systemctl --user start librus-monitor.service
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" status
systemctl --user start librus-sync.timer
systemctl --user list-timers librus-sync.timer
```

Restart wyłącznie WWW odczytuje tryb wczytywany raz na proces; odświeżenie monitoringu jest osobne. Uruchom timer wyłącznie wtedy, gdy był aktywny w stanie wyjściowym, a następnie wymagaj rzeczywistego pełnego godzinowego przebiegu aktywnego. Ręczne uruchomienie go nie zastępuje. Zachowaj regułę harmonogramu, alarmy i wyłączone kopie bazy.

Przy zmianie źródła lub wersji, ucznia, decyzji, dziennika, skrótu Calendar, niepełnym pobraniu albo błędzie analizy pozostaw timer zatrzymany i zachowaj bazę oraz dzienniki. Przywróć tylko chroniony tryb `preview`, zrestartuj WWW i odśwież monitoring; nie zmieniaj decyzji i nie przywracaj starej bazy. Usuń konkretną przyczynę, uzyskaj świeże kompletne podglądy i utwórz nowy plan pod nową ścieżką. Stary stan jest odrzucany; nie edytuj jego skrótu ani go nie nadpisuj. Odmowa blokady wymaga oczekiwania, nie równoległego procesu. Nie wznawiaj harmonogramu aktywnych zapisów po odmowie lub częściowym wykonaniu.

### Jawne lokalne porządki

Porządki są oddzielone od aktywacji i nie zmieniają Calendar. Używaj ich wyłącznie dla jawnie sprawdzonej ograniczonej listy nieaktualnych lokalnych propozycji. Przed ostatecznym planem aktywacji umieść prywatne żądanie 0600 w katalogu administracyjnym 0700, zgodne z poniższym schematem; rzeczywiste sprawdzone identyfikatory podaj prywatnie:

```json
{"changes":[{"proposal_id":"REVIEWED_PROPOSAL_ID","reason":"Sprawdzona lokalna propozycja jest nieaktualna; zachowaj Calendar i decyzje."}]}
```

```sh
cleanup_request="$source_admin_dir/cleanup-request.json"
cleanup_plan="$source_admin_dir/cleanup-$(date -u +%Y%m%dT%H%M%SZ).json"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" cleanup-existing --request-file "$cleanup_request" --plan-file "$cleanup_plan"
"$release_python" -I -m app.cli --config "$source_config" --state "$source_state" cleanup-existing --plan-file "$cleanup_plan" --apply
```

Sprawdź agregaty podglądu przed wykonaniem; wykonanie przyjmuje wyłącznie zapisany plan, bez `--request-file`. Zmiana stanu, zatwierdzone, edytowane i wykonane działania oraz dowód próby, trwającego lub nieznanego zapisu powodują odmowę. Jawne odrzucenia pozostają odrzucone; wycofywane są tylko dopuszczalne nieaktualne lokalne propozycje, z zachowaniem identyfikatorów, decyzji i dzienników. Zmieniony stan wymaga nowego przeglądu żądania i planu. Wykonaj porządki przed ostatecznym planem aktywacji i w razie potrzeby odśwież podgląd; nie obchodź odmowy przez SQL, wymianę bazy lub kasowanie wydarzeń Calendar.
