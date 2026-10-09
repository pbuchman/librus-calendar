# Demo offline i zrzuty ekranu

[English](demo.md) · [README](../README.pl.md)

Uruchom `scripts/ui_fixture.py` Pythonem ze środowiska wirtualnego projektu i otwórz `http://127.0.0.1:8795`. W razie potrzeby użyj `--port 8796`. Serwer nasłuchuje tylko lokalnie, tworzy nową tymczasową bazę SQLite i usuwa ją po zamknięciu. Korzysta z jawnej konfiguracji i ustalonego zegara, ignorując prywatną konfigurację, katalog domowy i nadpisania kont przez zmienne środowiskowe. Nie tworzy klienta Librusa, modelu Codex, połączenia Google Calendar, synchronizacji systemd ani zewnętrznego zapisu.

Wszyscy nauczyciele, wiadomości i wyniki analizy są fikcyjni. Zegar wskazuje **6 marca 2030, 10:15 Europe/Warsaw**. Wyświetlany sukces, kopie wydarzeń, cytaty i pochodzenie analizy są danymi demo, a nie dowodem działania usług. Każdy zrzut zawiera widoczne **„Demo — dane fikcyjne / fictional data”**.

| Zrzut | Źródło |
| --- | --- |
| `assets/inbox.png` | Rzeczywista skrzynka aplikacji, pięć fikcyjnych wiadomości napisanych od podstaw |
| `assets/message-detail.png` | Rzeczywisty szczegół wiadomości i fikcyjne rozpoznanie wyjścia do planetarium |
| `assets/extracurricular-review.png` | Rzeczywisty formularz decyzji o udziale w dobrowolnej robotyce |
| `assets/calendar-week.png` | Osobna lokalna ilustracja HTML na podstawie tych samych danych; **nie jest funkcją kalendarza aplikacji ani zrzutem Google Calendar** |

Ilustracja kalendarza zawiera szkolne wyjście, spotkanie rodziców i całodniowy termin dostarczenia zgody. Robotyka pozostaje niezatwierdzona i nie pojawia się na niej. Zatwierdzenie propozycji zmienia wyłącznie tymczasową bazę; funkcja synchronizacji demo niczego nie wykonuje.

## Odtwórz cztery obrazy

Zainstaluj Node.js i Playwright w osobnym katalogu zależności deweloperskich albo użyj istniejącej instalacji. Generator przyjmuje jawną ścieżkę modułu; nie dodaje zależności przeglądarki do produkcyjnych bibliotek Python.

```sh
mkdir -p /tmp/librus-demo-browser
npm install --prefix /tmp/librus-demo-browser playwright
/tmp/librus-demo-browser/node_modules/.bin/playwright install chromium
.venv/bin/python scripts/render_demo.py --playwright-module /tmp/librus-demo-browser/node_modules/playwright
```

Aby użyć istniejącego Chrome/Chromium, zamiast pobierania przeglądarki podaj `--browser-executable /absolute/path/to/browser`. `--node /absolute/path/to/node` wybiera środowisko Node. `--output DIRECTORY` zmienia katalog czterech obrazów, domyślnie `docs/assets`. `--qa-output DIRECTORY` określa osobny katalog zrzutu mobilnego i wyników JSON; domyślnie pozostają poza repozytorium, w katalogu tymczasowym.

Generator uruchamia izolowane demo, sprawdza tytuł, treść i oznaczenie strony, zapisuje widoki desktopowe 1440×1080, zatwierdza zajęcia dodatkowe i sprawdza zapis decyzji, wstrzymuje zapisy, żąda fikcyjnej synchronizacji i sprawdza nawigację do wiadomości przy 390×844. Blokuje nielokalne żądania przeglądarki i przerywa, jeśli którekolwiek zostanie podjęte lub wystąpi błąd przeglądarki. Następnie renderuje samodzielną ilustrację kalendarza. Piksele mogą nieznacznie zależeć od wersji przeglądarki i czcionek; dane i zegar są stałe.

Testy izolacji:

```sh
.venv/bin/python -m unittest discover -s tests -p test_demo.py -v
```

Sprawdzają brak odszukiwania konfiguracji/domowego katalogu oraz tworzenia rzeczywistych klientów, powtarzalność wiadomości, propozycji i wydarzeń, stały czas rozpoczęcia synchronizacji i brak niezatwierdzonej robotyki na ilustracji. Potwierdza to fikcyjne dane oraz przepływ interfejsu; zgodność rzeczywistych kont, modeli i kalendarza wymaga osobnych testów.
