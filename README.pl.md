# Librus Calendar

[English](README.md) · [Konfiguracja](docs/setup.pl.md) · [Demo](docs/demo.pl.md) · [Architektura i zabezpieczenia](docs/architecture.pl.md)

Prywatna skrzynka szkolna, która zamienia wiadomości z **Librus Synergia** na wpisy w Google Calendar. Osobna kolejka pozwala sprawdzić decyzje wymagające udziału rodzica.

Wiadomości są pobierane **bezpośrednio z Librusa**. Powiadomienia Gmail nie są źródłem danych ani wyzwalaczem. Interfejs aplikacji jest obecnie po polsku.

![Fikcyjna skrzynka w rzeczywistym interfejsie aplikacji](docs/assets/inbox.png)

*Demo — dane fikcyjne / fictional data. Wszystkie zrzuty przedstawiają napisane od podstaw fikcyjne wiadomości i nauczycieli.*

## Co robi aplikacja

- Przechowuje treści wiadomości, rozpoznane działania, pochodzenie analizy i kopie stanu wydarzeń w lokalnej bazie SQLite.
- Automatycznie kolejkuje jednoznaczne przyszłe wydarzenia szkolne, przypomnienia i terminy, gdy zapisy do kalendarza są włączone. Udział w dodatkowych zajęciach wymaga zatwierdzenia; niejasności, konflikty i odwołania trafiają do sprawdzenia.
- Dodaje jeden prefiks `[Librus]` w tytule. Opis zawiera cytat źródłowy, znacznik własności i **czas rozpoczęcia synchronizacji** w Europe/Warsaw. Jest to czas przygotowania trwałej operacji, a nie potwierdzenie zakończenia zapisu w kalendarzu.
- Zmienia wyłącznie własne wpisy w skonfigurowanym kalendarzu, nie wysyła zaproszeń i sprawdza wynik niepewnego zapisu przed ponowieniem.
- Udostępnia prywatną skrzynkę przez Tailscale, synchronizację co godzinę przez systemd, codzienne kopie SQLite i opcjonalny monitoring Netdata.

## Uruchom lokalnie

Wymagany Python **3.12+**. Demo nie wymaga danych logowania do Librusa, logowania Codex, podłączonego kalendarza ani konfiguracji konta.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/ui_fixture.py
```

Otwórz [http://127.0.0.1:8795](http://127.0.0.1:8795). Rzeczywisty interfejs działa na tymczasowej bazie z ustalonym zegarem w marcu 2030. Zatwierdzanie i przyciski synchronizacji dotyczą wyłącznie danych demo; zamknięcie procesu usuwa bazę. Nie następują wywołania Librusa, AI ani zewnętrznego kalendarza. Szczegóły: [odtwarzanie demo](docs/demo.pl.md).

## Wiadomość, decyzja, wpis w kalendarzu

![Rzeczywisty widok wiadomości z fikcyjnym rozpoznanym wydarzeniem](docs/assets/message-detail.png)

*Rzeczywisty interfejs aplikacji. Wynik rozpoznania jest przygotowanymi danymi fikcyjnymi; model nie był wywoływany.*

![Rzeczywisty widok zatwierdzania fikcyjnych zajęć dodatkowych](docs/assets/extracurricular-review.png)

*Dobrowolne warsztaty robotyki czekają na potwierdzenie udziału.*

![Lokalna ilustracja kalendarza z tymi samymi danymi fikcyjnymi](docs/assets/calendar-week.png)

*Lokalna ilustracja wpisów z tego samego tygodnia — **nie jest funkcją kalendarza aplikacji ani zrzutem Google Calendar**. Nie ma na niej niezatwierdzonych warsztatów robotyki.*

## Użycie z własnymi kontami

Rzeczywista synchronizacja wymaga konta Librus, obsługiwanego i zalogowanego środowiska Codex CLI, dostępnych modeli analizy i narzędzi, połączonej aplikacji Google Calendar oraz prawa zapisu do dokładnie wskazanego kalendarza. Te elementy **nie są dostarczane z projektem**; nazwy modeli, identyfikatory konektorów i narzędzia mogą zależeć od konta i środowiska. Nie jest to samodzielna integracja OAuth. Zacznij od [instrukcji konfiguracji i wdrożenia](docs/setup.pl.md).

Zapisy są początkowo wstrzymane. Pierwsze `sync --dry-run` może nadal czytać Librusa i wywoływać AI oraz narzędzia odczytu kalendarza; nie jest to demo offline. Sprawdź rozpoznane działania i wynik testu środowiska przed włączeniem zapisów.

## Prywatność i ograniczenia

To **nieoficjalna**, niezależna integracja, niezwiązana z Librusem, Google ani OpenAI. Korzysta z nieudokumentowanych interfejsów logowania i wiadomości Librusa, które mogą się zmienić. Obsługuje wiadomości, a nie cały plan lekcji, oceny czy wszystkie funkcje szkoły; zawartość załączników nie jest analizowana.

Treść wiadomości jest prywatna. Rzeczywista analiza przesyła przekazaną treść i kontekst zapisanych wydarzeń do skonfigurowanej usługi AI; zapis w Google Calendar przekazuje wybrane szczegóły wydarzeń i cytaty źródłowe. Przechowuj dane logowania, bazę i kopie poza repozytorium, z ograniczonymi uprawnieniami, a interfejs udostępniaj przez Tailscale wyłącznie właścicielowi. Zobacz [prywatność i granice działania](docs/architecture.pl.md).

AI może błędnie zinterpretować daty i kontekst. Testy automatyczne oraz demo offline nie potwierdzają zgodności z Twoją szkołą, kontem Codex, Google Calendar ani działającym wdrożeniem. Kontroluj pierwsze wyniki i wykonuj kopie zapasowe.

## Rozwój

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Projekt powstał jako osobisty prototyp. Wersja publiczna zawiera konfigurowalny kod, ogólne przykłady wdrożenia i fikcyjne dane; wyklucza produkcyjne dane logowania, wiadomości, stan i prywatne materiały z wdrożenia. Zobacz [architekturę](docs/architecture.pl.md) i wewnętrzny [kontrakt aplikacji](app/CONTRACT.md).

Licencja [MIT](LICENSE).
