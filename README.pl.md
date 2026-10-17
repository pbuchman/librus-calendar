# Librus Calendar

[English](README.md) · [Konfiguracja](docs/setup.pl.md) · [Demo](docs/demo.pl.md) · [Architektura i zabezpieczenia](docs/architecture.pl.md)

Prywatna skrzynka szkolna, która zamienia wiadomości, ogłoszenia i wpisy terminarza z **Librus Synergia** na działania w Google Calendar. Osobna kolejka pozwala sprawdzić decyzje wymagające udziału rodzica.

Wiadomości są pobierane **bezpośrednio z Librusa**. Powiadomienia Gmail nie są źródłem danych ani wyzwalaczem. Interfejs aplikacji jest obecnie po polsku.

![Fikcyjna skrzynka w rzeczywistym interfejsie aplikacji](docs/assets/inbox.png)

*Demo — dane fikcyjne / fictional data. Wszystkie zrzuty przedstawiają napisane od podstaw fikcyjne wiadomości i nauczycieli.*

## Co robi aplikacja

- Przechowuje treści wiadomości, rozpoznane działania, pochodzenie analizy i kopie stanu wydarzeń w lokalnej bazie SQLite.
- Łączy jawnie alternatywne terminy jednej dobrowolnej oferty w jedną kartę. Wybierz jeden termin bez domyślnego zaznaczenia albo odrzuć całą ofertę. Minione terminy pozostają widoczne i nieaktywne; wybór zamyka pozostałe opcje. Spotkania cykliczne i odrębne obowiązki pozostają osobnymi decyzjami.
- Automatycznie kolejkuje jednoznaczne przyszłe wydarzenia szkolne, przypomnienia i terminy, gdy zapisy do kalendarza są włączone. Udział w dodatkowych zajęciach wymaga zatwierdzenia; niejasności, konflikty i odwołania trafiają do sprawdzenia.
- Dodaje jeden prefiks `[Librus]` w tytule. Opis zawiera cytat źródłowy, znacznik własności i **czas rozpoczęcia synchronizacji** w Europe/Warsaw. Jest to czas przygotowania trwałej operacji, a nie potwierdzenie zakończenia zapisu w kalendarzu.
- Zmienia wyłącznie własne wpisy w skonfigurowanym kalendarzu, nie wysyła zaproszeń i sprawdza wynik niepewnego zapisu przed ponowieniem.
- Udostępnia prywatną skrzynkę przez Tailscale, synchronizację co godzinę przez systemd i opcjonalny monitoring Netdata.
- Pokazuje ostatnie sprawdzenie dostępu do kalendarza i odzyskanie po ograniczonym ponowieniu odczytu; Netdata prezentuje próby ponowienia i odzyskania z ostatnich 24 godzin.

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

*Jedna fikcyjna oferta szkolenia pierwszej pomocy przedstawia terminy do wyboru; miniony termin jest widoczny i nieaktywny. Żaden termin nie jest domyślnie zaznaczony.*

![Lokalna ilustracja kalendarza z tymi samymi danymi fikcyjnymi](docs/assets/calendar-week.png)

*Lokalna ilustracja wpisów z tego samego tygodnia — **nie jest funkcją kalendarza aplikacji ani zrzutem Google Calendar**. Nie ma na niej niezatwierdzonych warsztatów robotyki.*

## Użycie z własnymi kontami

Rzeczywista synchronizacja wymaga konta Librus, obsługiwanego i zalogowanego środowiska Codex CLI, dostępnych modeli analizy i narzędzi, połączonej aplikacji Google Calendar oraz prawa zapisu do dokładnie wskazanego kalendarza. Te elementy **nie są dostarczane z projektem**; nazwy modeli, identyfikatory konektorów i narzędzia mogą zależeć od konta i środowiska. Nie jest to samodzielna integracja OAuth. Zacznij od [instrukcji konfiguracji i wdrożenia](docs/setup.pl.md).

Zapisy są początkowo wstrzymane. Pierwsze `sync --dry-run` może nadal czytać Librusa i wywoływać AI oraz narzędzia odczytu kalendarza; nie jest to demo offline. Sprawdź rozpoznane działania i wynik testu środowiska przed włączeniem zapisów.

Analiza i narzędzia kalendarza domyślnie używają `gpt-6.1-sol` z rozumowaniem `high`. Wyłącznie początkowe odczytowe sprawdzenie listy kalendarzy może być ponowione raz po pięciu sekundach przy zweryfikowanej nieoczekiwanej liczbie zakończonych wywołań narzędzia (zero lub wiele). Ta zasada nie ponawia zapisów; niepewny zapis wymaga sprawdzenia wyniku w trybie odczytu. Zobacz [sprawdzanie kalendarza i bezpieczną diagnostykę](docs/setup.pl.md#sprawdzanie-kalendarza-i-bezpieczna-diagnostyka).

## Prywatność i ograniczenia

To **nieoficjalna**, niezależna integracja, niezwiązana z Librusem, Google ani OpenAI. Korzysta z nieudokumentowanych interfejsów logowania i wiadomości Librusa, które mogą się zmienić. Obsługuje wiadomości, ogłoszenia, wpisy terminarza i informacyjne zastępstwa w zakresie dostępnym dla uwierzytelnionego ucznia. Nie importuje całego planu lekcji, ocen ani wszystkich funkcji szkoły. Treść załączników, obrazy, OCR i PDF-y nie są analizowane ani wysyłane do usług zewnętrznych.

Treść wiadomości jest prywatna. Rzeczywista analiza przesyła przekazaną treść i kontekst zapisanych wydarzeń do skonfigurowanej usługi AI; zapis w Google Calendar przekazuje wybrane szczegóły wydarzeń i cytaty źródłowe. Przechowuj dane logowania, bazę i kopie poza repozytorium, z ograniczonymi uprawnieniami, a interfejs udostępniaj przez Tailscale wyłącznie właścicielowi. Zobacz [prywatność i granice działania](docs/architecture.pl.md).

AI może błędnie zinterpretować daty i kontekst. Testy automatyczne oraz demo offline nie potwierdzają zgodności z Twoją szkołą, kontem Codex, Google Calendar ani działającym wdrożeniem. Kontroluj pierwsze wyniki i zachowuj dziennik operacji. Automatyczne kopie bazy są celowo wyłączone.

## Rozwój

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Projekt powstał jako osobisty prototyp. Wersja publiczna zawiera konfigurowalny kod, ogólne przykłady wdrożenia i fikcyjne dane; wyklucza produkcyjne dane logowania, wiadomości, stan i prywatne materiały z wdrożenia. Zobacz [architekturę](docs/architecture.pl.md) i wewnętrzny [kontrakt aplikacji](app/CONTRACT.md).

Licencja [MIT](LICENSE).

## Wiele źródeł Librusa

Nowe źródła domyślnie mają `multi_source_mode=off`. W trybie `preview` ogłoszenia i terminarz trafiają do osobnej kolejki analizy tylko do odczytu; nie mogą zatwierdzać propozycji, zmieniać decyzji rodzica ani zapisywać w Calendar. Aktywacja wymaga świeżego pobrania i walidacji. W trybie `active` zweryfikowane źródła mogą zostać połączone z jednym wspólnym działaniem zamiast tworzyć duplikat wpisu. Dopasowanie semantyczne sprawdza kompletny ograniczony zbiór kandydatów i niezależnie weryfikuje dosłowne dowody. Brakujący starszy dowód może być osobno odtworzony z pochodzenia o dokładnie zgodnych skrótach, bez wymyślania historycznego cytatu. Starsze źródło z nieznanym adresatem może jedynie dodać oznaczone potwierdzenie jednoznacznie zweryfikowanego wystąpienia zatwierdzonego przez rodzica i powiązanego z własnym wydarzeniem; nie uprawnia do zapisów ani przyszłego dopasowania. Niepewne powiązania trafiają do sprawdzenia. Ponowne przetworzenie zachowuje identyfikatory wydarzeń i decyzje.

Dla zwykłych działań szkolnych zweryfikowany widok terminarza klasy, udostępniony w uwierzytelnionym kontekście wybranego ucznia, może potwierdzić adresatów klasowych nawet bez powtórzenia klasy w treści. Ten kontekst jest zapisywany oddzielnie od dosłownych dowodów działania. Nie potwierdza wyłącznej roli konta, nie rozstrzyga nieznanego uczestnictwa, nie zastępuje innego adresata i nie zmienia decyzji rodzica. Załączniki nadal są wyłączone z analizy.

Panel **Wszystkie źródła** filtruje wiadomości, ogłoszenia i terminarz, pokazuje wynik i powód dla źródła oraz prowadzi do wspólnego działania ze wszystkimi źródłami i oceną pewności. **Podgląd źródeł** pozostaje osobny od decyzji. Zwykłe szkolne przypomnienia mogą być oznaczone jako opcjonalne; zajęcia dodatkowe i nieznany udział wymagają sprawdzenia. Warunkowe informacje bez daty pozostają informacją. Załączniki mają etykietę **załącznik nieanalizowany**. Zobacz [tryby źródeł i odbiór](docs/setup.pl.md#wdrożenie-wielu-źródeł).
