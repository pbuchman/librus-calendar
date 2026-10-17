# Demo offline i zrzuty ekranu

[English](demo.md) · [README](../README.pl.md)

Uruchom `scripts/ui_fixture.py` Pythonem ze środowiska wirtualnego projektu i otwórz `http://127.0.0.1:8795`. W razie potrzeby użyj `--port 8796`. Serwer nasłuchuje tylko lokalnie, tworzy nową tymczasową bazę SQLite i usuwa ją po zamknięciu. Korzysta z jawnej konfiguracji i ustalonego zegara, ignorując prywatną konfigurację, katalog domowy i nadpisania kont przez zmienne środowiskowe. Nie tworzy klienta Librusa, modelu Codex, połączenia Google Calendar, synchronizacji systemd ani zewnętrznego zapisu.

Wszyscy nauczyciele, wiadomości i wyniki analizy są fikcyjni. Zegar wskazuje **6 marca 2030, 10:15 Europe/Warsaw**. Wyświetlany sukces, kopie wydarzeń, cytaty i pochodzenie analizy są danymi demo, a nie dowodem działania usług. Każdy zrzut zawiera widoczne **„Demo — dane fikcyjne / fictional data”**.

| Zrzut | Źródło |
| --- | --- |
| `assets/inbox.png` | Rzeczywista skrzynka aplikacji, siedem fikcyjnych wiadomości napisanych od podstaw |
| `assets/message-detail.png` | Rzeczywisty szczegół wiadomości i fikcyjne rozpoznanie wyjścia do planetarium |
| `assets/extracurricular-review.png` | Rzeczywista karta wyboru terminu jednej oferty szkolenia pierwszej pomocy, w tym miniony nieaktywny termin |
| `assets/calendar-week.png` | Osobna lokalna ilustracja HTML na podstawie tych samych danych; **nie jest funkcją kalendarza aplikacji ani zrzutem Google Calendar** |

Ilustracja kalendarza zawiera szkolne wyjście, spotkanie rodziców i całodniowy termin dostarczenia zgody. Robotyka pozostaje niezatwierdzona i nie pojawia się na niej. Zatwierdzenie propozycji zmienia wyłącznie tymczasową bazę; funkcja synchronizacji demo niczego nie wykonuje.

## Wypróbuj jedną ofertę z terminami do wyboru

Otwórz `/review` lub `/messages/demo-training-choice`. Fikcyjne szkolenie pierwszej pomocy ma trzy terminy, w tym jeden miniony — nadal widoczny, ale nieaktywny. Żaden nie jest domyślnie zaznaczony. Wybierz przyszły termin i naciśnij **Dodaj wybrany termin**, aby zakolejkować tylko ten wybór; karta pokaże wybrany termin i zamknie pozostałe opcje. **Odrzuć całą ofertę** odrzuca wszystkie opcje bez zaznaczania terminu. Rozstrzygnięta oferta znika z kolejki decyzji; stan wyboru albo odrzucenia pozostaje w szczególe wiadomości źródłowej. Licznik nawigacji liczy jedną otwartą ofertę zamiast każdej daty. Jeśli wybrany termin później wymaga sprawdzenia po konflikcie zapisu, do kolejki wraca wyłącznie ten termin z kontrolowanym zatwierdzeniem lub odrzuceniem; pozostałe opcje pozostają zamknięte. Niejednoznaczne nowe propozycje po wcześniejszej decyzji o ofercie pokazują kontekst źródłowy i pozwalają na odrzucenie, a zatwierdzenie jest zablokowane. Odświeżenie zachowuje decyzję w bieżącym procesie demo; ponowne uruchomienie zeruje fikcyjne dane.

Osobna wiadomość `/messages/demo-training-series` opisuje dwa spotkania cyklu teatralnego. Pozostają zwykłymi osobnymi propozycjami; cykl nie jest wyborem alternatywnych dat. Istniejąca robotyka nadal wymaga zwykłego potwierdzenia udziału. Rozszerzone demo zawiera osiem fikcyjnych wiadomości napisanych od podstaw, w tym zachowane odrzucenie klubu. Dotychczasowe zrzuty pochodzą z wcześniejszej wersji z siedmioma wiadomościami.

Odczyt `/api/proposals` zachowuje dotychczasowy limit. `/api/proposal-groups` zwraca komplet opcji oferty niezależnie od tego limitu. Decyzja korzysta z `POST /api/proposal-groups/{id}/decision`, pól `decision=select|reject`, `proposal_id` przy wyborze i `expected_revision`, nagłówka tego samego źródła oraz ciasteczka i pola CSRF. Decyzja o pojedynczej propozycji nie omija zasady jednego wyboru. Zmiana źródła, nieaktualna wersja, miniony termin i sprzeczna decyzja o zamkniętej ofercie są odrzucane przez bazę aplikacji.

## Odtwórz cztery obrazy

Zainstaluj Node.js i Playwright w osobnym katalogu zależności deweloperskich albo użyj istniejącej instalacji. Generator przyjmuje jawną ścieżkę modułu; nie dodaje zależności przeglądarki do produkcyjnych bibliotek Python.

```sh
mkdir -p /tmp/librus-demo-browser
npm install --prefix /tmp/librus-demo-browser playwright
/tmp/librus-demo-browser/node_modules/.bin/playwright install chromium
.venv/bin/python scripts/render_demo.py --playwright-module /tmp/librus-demo-browser/node_modules/playwright
```

Aby użyć istniejącego Chrome/Chromium, zamiast pobierania przeglądarki podaj `--browser-executable /absolute/path/to/browser`. `--node /absolute/path/to/node` wybiera środowisko Node. `--output DIRECTORY` zmienia katalog czterech obrazów, domyślnie `docs/assets`. `--qa-output DIRECTORY` określa osobny katalog zrzutu mobilnego i wyników JSON; domyślnie pozostają poza repozytorium, w katalogu tymczasowym.

Generator uruchamia izolowane demo, sprawdza tytuł, treść i oznaczenie strony, zapisuje widoki desktopowe 1440×1080, zatwierdza osobną propozycję robotyki, sprawdza komplet alternatywnych terminów bez domyślnego wyboru i z nieaktywną minioną datą, wybiera jeden przyszły termin i sprawdza zamknięty stan w szczególe wiadomości, wstrzymuje zapisy, żąda fikcyjnej synchronizacji i sprawdza nawigację do wiadomości przy 390×844. Blokuje nielokalne żądania przeglądarki i przerywa, jeśli którekolwiek zostanie podjęte lub wystąpi błąd przeglądarki. Następnie renderuje samodzielną ilustrację kalendarza. Piksele mogą nieznacznie zależeć od wersji przeglądarki i czcionek; dane i zegar są stałe.

Testy izolacji:

```sh
.venv/bin/python -m unittest discover -s tests -p test_demo.py -v
```

Sprawdzają brak odszukiwania konfiguracji/domowego katalogu oraz tworzenia rzeczywistych klientów, powtarzalność wiadomości, propozycji i wydarzeń, stały czas rozpoczęcia synchronizacji i brak niezatwierdzonej robotyki na ilustracji. Potwierdza to fikcyjne dane oraz przepływ interfejsu; zgodność rzeczywistych kont, modeli i kalendarza wymaga osobnych testów.

## Widoki źródeł i podglądu

Otwórz `/sources`, aby filtrować fikcyjne wiadomości, ogłoszenia i terminarz oraz przejść ze źródła do wspólnego działania. Szczegół działania pokazuje źródła, dowody i pewność. `/preview` przedstawia osobne fikcyjne analizy bez przycisku zatwierdzania ani aktywacji. Załączniki mają etykietę **załącznik nieanalizowany** i nie są pobierane. Przygotowane wyniki służą wyłącznie demonstracji; dotychczasowe cztery zrzuty powstały przed dodaniem widoków źródeł i nie potwierdzają ich wdrożenia.

Podgląd pokazuje planowane dopasowania, zachowane decyzje rodzica i różnice wobec obecnych działań. Fikcyjna powtórzona wycieczka wskazuje istniejące działanie; powtórzone zaproszenie do klubu zachowuje rzeczywiste odrzucenie zapisane w fikcyjnej bazie. Wirtualne działania podglądu nie mają odnośnika do aktywnego działania. Gotowy podgląd bieżącej wersji źródła ma osobną etykietę; stary podgląd nie zastępuje stanu bieżącego źródła.

Te fikcyjne widoki nie sprawdzają uwierzytelnionego pochodzenia dowodu adresatów terminarza klasy, odzyskiwania starszych dowodów, niezależnej jednoznacznej weryfikacji ani blokady przerwanej aktywacji. Takie mechanizmy wymagają osobnych testów; zapisany podgląd i dodatkowe źródło bez potwierdzonego adresata nie uprawniają do zapisów Calendar.
