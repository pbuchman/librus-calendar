# Konfiguracja własnej instancji

[English](setup.md) · [README](../README.pl.md)

[Demo offline](demo.pl.md) pozwala najszybciej zobaczyć interfejs. Poniższe kroki uruchamiają dostęp do rzeczywistych kont i nie są wykonywane przez demo.

## Wymagania

- Python 3.12+, osobne środowisko wirtualne i wersje bibliotek z `requirements.txt`.
- Konto Librus Synergia z dostępem do wymaganych wiadomości.
- Zalogowany Codex CLI na komputerze wykonującym synchronizację, obsługujący opcje używane w `app/codex_runtime.py`. Kod uruchamia `codex exec` z odizolowanymi instrukcjami i ściśle wybranymi narzędziami; nie każde wydanie CLI lub konto obsługuje tę kombinację.
- Połączenie Google Calendar dostępne dla tego samego konta Codex i środowiska, udostępniające obsługiwane narzędzia listy, wyszukiwania, odczytu, tworzenia, aktualizacji i usuwania. Dokładny identyfikator konektora trzeba skonfigurować dla własnej instalacji.
- Uprawnienia writer lub owner do dokładnie wskazanego kalendarza. `calendar_owner` oznacza zweryfikowany główny e-mail zalogowanego właściciela, a nie identyfikator dodatkowego kalendarza.
- Do działania w tle: Linux z usługami użytkownika systemd, Tailscale Serve z zaufanymi nagłówkami tożsamości i opcjonalnie kolektor Python Netdata.

Przykładowe modele to `gpt-6.1-sol`/`high` do analizy i `gpt-6-luna`/`medium` do narzędzi kalendarza. Są to wartości domyślne kodu, a nie gwarancja dostępności. Wybierz model i poziom rozumowania obsługiwany przez własne środowisko; repozytorium nie tworzy ani nie nadaje połączenia konektora.

## Prywatna konfiguracja

Użyj `deploy/config.json.example` jako wzoru dla `~/.config/librus-calendar/config.json`. Każdy dostarczony plik JSON musi być zwykłym plikiem należącym do użytkownika, z uprawnieniami **0600**, w prywatnym katalogu. Konfiguracja i hasła pozostają poza repozytorium. Zastąp wszystkie fikcyjne wartości przed rzeczywistą synchronizacją.

| Pole | Znaczenie |
| --- | --- |
| `calendar_id` | Dokładny identyfikator Google Calendar, również dodatkowego kalendarza |
| `calendar_owner` | Zweryfikowany główny e-mail zalogowanego właściciela; kontrola bezpieczeństwa uczestników |
| `calendar_connector` | Identyfikator `connector_...` połączonej aplikacji |
| `tailscale_owner` | Jedyna dozwolona tożsamość logowania Tailscale |
| `allowed_hosts` | Dokładny host Tailscale i port; bez wieloznaczników |
| `allow_local` | Dla prywatnego interfejsu produkcyjnego pozostaw `false` |
| `state_path`, `credentials_path` | Prywatne lokalizacje bazy i danych logowania; domyślnie w katalogu domowym |
| `codex_binary` | Ścieżka programu lub `codex` dostępny w PATH usługi |
| `analysis_model`, `analysis_effort` | Model analizy i obsługiwany poziom rozumowania |
| `tool_model`, `tool_effort` | Model narzędzi kalendarza i obsługiwany poziom rozumowania |
| `timeout` | Limit pojedynczego wywołania środowiska w sekundach: 1–300 |
| `monitor_url`, `monitor_output` | Opcjonalny lokalny endpoint stanu i plik zbiorczych danych monitoringu |

`LIBRUS_CONFIG_FILE` wskazuje inny plik JSON; jawnie wskazany brakujący plik powoduje błąd. Puste `LIBRUS_CONFIG_FILE` wyłącza odszukiwanie JSON. `LIBRUS_<POLE_WIELKIMI_LITERAMI>` nadpisuje pole, np. `LIBRUS_CALENDAR_ID`, `LIBRUS_CODEX_BINARY`, `LIBRUS_ANALYSIS_MODEL`. `LIBRUS_ALLOWED_HOSTS` jest listą rozdzieloną przecinkami, a `LIBRUS_ALLOW_LOCAL` przyjmuje `0` lub `1`. Nieznane pola JSON i błędne ustawienia są odrzucane. Nie ma domyślnych danych osobistego konta.

Utwórz plik danych logowania w chronionym edytorze, w postaci:

```json
{"login": "YOUR_LIBRUS_LOGIN", "password": "<YOUR_LIBRUS_PASSWORD>"}
```

Użyj prywatnej ścieżki `credentials_path` i uprawnień 0600. Nie umieszczaj hasła w argumencie polecenia, historii powłoki, repozytorium ani na zrzucie ekranu. `scripts/import_credentials.py` jest opcjonalnym narzędziem migracji istniejącego pliku tymczasowego `login hasło`, należącego do użytkownika: sprawdza chroniony plik docelowy przed usunięciem źródła i nie loguje danych logowania.

## Sprawdź przed włączeniem zapisów

Uruchom z repozytorium, używając Pythona ze środowiska wirtualnego:

```sh
.venv/bin/python -m app.cli status
.venv/bin/python -m scripts.probe_runtime
.venv/bin/python -m app.cli sync --dry-run
```

Test środowiska wykonuje **rzeczywisty odczyt kalendarza** i sprawdza dostęp do wskazanego kalendarza. Dry-run może pobierać prywatne wiadomości Librusa i wysyłać je do usługi analizy; pomija zapisy. Nie działa offline. Sprawdź lokalne propozycje i kolejkę decyzji. Nowa baza ma początkowo wstrzymane zapisy. Po sprawdzeniu konta, wiadomości źródłowych, dat, modelu i działania konektora:

```sh
.venv/bin/python -m app.cli pause-writes --resume
.venv/bin/python -m app.cli sync
```

Aby ponownie wstrzymać zapisy, uruchom `pause-writes` bez `--resume`. Nie zatrzymuje to pobierania ani analizy. CLI obsługuje również `--state PATH` przed podpoleceniem, `sync --credentials PATH --limit N` (1–100) i `backup --destination PATH`. Prywatna konfiguracja nadal obowiązuje, chyba że zastąpią ją jawne ustawienia środowiska.

## Usługi Linux i prywatny dostęp WWW

Dostarczone jednostki zakładają katalog **`~/librus-calendar`** i środowisko wirtualne wewnątrz niego. Uruchom tam `bash scripts/install_linux.sh`. Instalator kopiuje jednostki synchronizacji, WWW i backupu oraz przykłady konfiguracji, odmawia nadpisania innych istniejących jednostek i **nie uruchamia usług**. Przygotuj właściwe `config.json`, `credentials.json` i `web.env` w `~/.config/librus-calendar`. Ustaw dokładną ścieżkę Codex w `codex_binary`; PATH systemd może być inny niż w interaktywnej powłoce.

`web.env` określa dokładne `LIBRUS_TAILSCALE_OWNER`, `LIBRUS_ALLOWED_HOSTS` oraz `LIBRUS_ALLOW_LOCAL=0`. Serwer WWW nasłuchuje tylko na `127.0.0.1:8795`, ignoruje przekazane adresy klienta i ufa wyłącznie lokalnemu proxy Tailscale z właściwą tożsamością właściciela. Nie udostępniaj portu przez publiczne proxy ani Tailscale Funnel. Konfiguracja ACL i dostępu do konta Tailscale należy do administratora instancji.

Po skonfigurowaniu i sprawdzeniu instancji ustaw Tailscale Serve i włącz jednostki:

```sh
tailscale serve --bg --https=8445 --yes http://127.0.0.1:8795
systemctl --user enable --now librus-web.service librus-sync.timer librus-backup.timer
systemctl --user list-timers
```

Synchronizacja działa o każdej pełnej godzinie Europe/Warsaw; harmonogram nadrabia pominięte uruchomienia. Cały proces ma limit 45 minut. Backup wykonywany jest codziennie o 03:15 Europe/Warsaw. Domyślna retencja obejmuje siedem najnowszych kopii; każda przechodzi kontrolę integralności SQLite. Kopie zawierają prywatne wiadomości i szczegóły wydarzeń. W razie potrzeby zapewnij ochronę hosta, szyfrowanie, kopię zewnętrzną i trwałość sesji użytkownika systemd.

## Opcjonalny monitoring Netdata

`deploy/install-librus-monitor.sh APP_USER` instaluje kolektor i reguły zdrowia jako root; wymaga istniejącej grupy `netdata` i katalogu kolektora Python. Nie restartuje Netdata ani nie włącza monitora użytkownika. Przeczytaj skrypt przed uruchomieniem z prawami administratora.

Skopiuj usługę i timer monitora do katalogu jednostek użytkownika aplikacji, a `deploy/monitor.env.example` do jego `~/.config/librus-calendar/monitor.env`, z uprawnieniami 0600. Zastąp fikcyjną tożsamość i host Tailscale wartościami używanymi przez proxy WWW. Pozostaw puste `LIBRUS_CONFIG_FILE=`: monitor eksportuje zbiorczy stan bazy w trybie odczytu i nie ma dostępu do prywatnych katalogów konfiguracji oraz danych logowania. Włącz `librus-monitor.timer`, a następnie przeładuj Netdata metodą właściwą dla swojej instalacji.

Eksporter działa co minutę i zapisuje zbiorcze czasy, liczby elementów kolejek, stany techniczne i zdrowie lokalnych usług do `/var/lib/librus-monitor/status.json`. Nie eksportuje treści wiadomości ani haseł. Alarmy błędów, wieku kolejki i backupów informują o stanie technicznym; nie potwierdzają poprawności kalendarza ani skuteczności rzeczywistych zapisów.

## Diagnoza problemów i odtwarzanie kopii

Sprawdź `.venv/bin/python -m app.cli status`, `systemctl --user list-timers` oraz `systemctl --user status librus-sync.service librus-web.service`. `journalctl --user -u librus-sync.service -n 50` pokazuje ostatnie logi procesu. Traktuj status i logi jako prywatne; usuń identyfikatory kont przed udostępnieniem. Porównaj ostatni pełny sukces z ostatnią próbą, zaległościami i niepewnymi operacjami. Samo wstrzymanie zapisów lub zakończenie timera nie dowodzi skutecznej synchronizacji.

Uruchom `.venv/bin/python -m app.cli backup`, aby wykonać prywatną kopię z kontrolą integralności, albo wskaż `backup --destination /PRIVATE/NEW-BACKUP.sqlite3`. Istniejące pliki docelowe nie są nadpisywane. Sprawdź wybraną kopię przez `sqlite3 /PRIVATE/BACKUP.sqlite3 'PRAGMA integrity_check;'`; oczekiwany wynik to `ok`. Potwierdza to integralność SQLite, a nie zgodność z aktualnym zewnętrznym kalendarzem.

Przed odtworzeniem wstrzymaj zapisy i zatrzymaj timer synchronizacji, proces, usługę WWW oraz timer backupu. Zachowaj aktualną bazę **wraz z dziennikiem operacji** jako osobną sprawdzoną kopię. Odtwarzaj tylko wtedy, gdy żaden proces nie może zapisywać bazy; zachowaj właściciela i uprawnienia 0600. Przed uruchomieniem usług wykonaj `pause-writes` na odtworzonej bazie, ponieważ kopia mogła zawierać włączone zapisy. Porównaj ślady wydarzeń i operacji z bieżącym kalendarzem oraz sprawdź niepewne wyniki przed wznowieniem zapisów. Cofnięcie SQLite w ciemno po zewnętrznych zapisach może usunąć dowody zapobiegające duplikatom i spowodować powtórzenia lub konflikty; odtworzenie bazy nie cofa kalendarza.

## Polecenia administracyjne

`reanalyze --message-id ID` podgląda wynik ponownej analizy wskazanych lokalnych wiadomości; `--apply` zastępuje tylko nietknięte, kwalifikujące się propozycje. Nie loguje się do Librusa i nie zapisuje kalendarza. `metadata-refresh --event-id ID` podgląda zmianę prefiksu i czasu synchronizacji; `--apply` może aktualizować wskazane istniejące wydarzenia zewnętrzne. Oba wymagają jawnych identyfikatorów i zachowują dziennik operacji. Przed obsługą przeczytaj [kontrakt aplikacji](../app/CONTRACT.md). Niepewny wynik wymaga sprawdzenia, a nie ponawiania w ciemno czy cofania bazy.
