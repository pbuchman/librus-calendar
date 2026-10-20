# Architektura, zabezpieczenia i prywatność

[English](architecture.md) · [README](../README.pl.md) · [Konfiguracja](setup.pl.md)

System pobiera listy i treść wiadomości bezpośrednio z Librus Synergia przez HTTPS. Nie przetwarza powiadomień Gmail. Lokalna baza SQLite rozdziela wykrywanie, pobieranie całych wiadomości, analizę AI, propozycje działań, trwałe operacje, zweryfikowane kopie wydarzeń i historię stanu technicznego.

`app/librus_client.py` obsługuje wąski przepływ logowania i wiadomości; `app/sync.py` koordynuje proces z blokadą; `app/codex_runtime.py` wywołuje zalogowany Codex CLI do analizy strukturalnej i wybranych narzędzi kalendarza. `app/core.py` wymusza zasady zapisu poza modelem. Skrzynka FastAPI/Jinja w `app/web.py` pokazuje lokalne wiadomości, rozpoznane działania, zapisane wydarzenia i kolejkę sprawdzenia. Aplikacja nie udostępnia tygodniowej siatki kalendarza; siatka w dokumentacji jest wyraźnie oznaczoną osobną ilustracją fikcyjnych danych.

## Decyzje i bezpieczeństwo zapisu

Jawnie alternatywne terminy jednej dobrowolnej oferty są zapisywane jako jedna trwała grupa powiązana z wiadomością źródłową. Kolejka decyzji i szczegół wiadomości pokazują komplet opcji niezależnie od limitu starszej listy propozycji; żaden termin nie jest domyślnie wybrany. Minione daty pozostają widoczne, ale nie można ich zaznaczyć. Wybór przyszłego terminu atomowo kolejkuje tę opcję i zamyka pozostałe; odrzucenie zamyka całą ofertę. Sprawdzenie wersji i źródła chroni przed nieaktualną decyzją; działanie na pojedynczej propozycji nie omija grupy. Zamknięta grupa zachowuje stan wyboru albo odrzucenia. Cykl spotkań i osobne obowiązki nie są łączone wyłącznie na podstawie wspólnej wiadomości lub tytułu.

Jednoznaczne przyszłe działania szkolne z potwierdzeniem w źródle mogą być automatyczne, gdy zapisy są włączone. Przygotowanie do szkoły, zajęcia klasowe i terminy oceniane są osobno dla każdego działania. Tworzenie dobrowolnych zajęć dodatkowych wymaga decyzji o udziale nawet przy jasnej dacie; nieznany zakres, niejednoznaczne daty, nakładające się lub powielone początki, niebezpieczny stan wydarzenia i odwołania wymagają sprawdzenia. Sama analiza nie może zatwierdzić odwołania ani uznać, że dziecko zapisano na dodatkowe zajęcia.

Daty używają Europe/Warsaw i rozróżniają wydarzenia godzinowe od całodniowych przypomnień i terminów. Dokładna godzina deadline'u pozostaje w tytule i opisie, a zapis w kalendarzu obejmuje jeden cały dzień. Błędne i niejednoznaczne godziny zmiany czasu są odrzucane. Dla wydarzenia godzinowego bez końca może zostać użyty wyraźnie opisany domyślny czas jednej godziny.

Każdy nowy tytuł otrzymuje jeden prefiks `[Librus]`. Opis przechowuje cytat, odwołanie do wiadomości, znacznik własności i zamrożony czas rozpoczęcia synchronizacji danej operacji. To czas przygotowania, a nie zakończenia zapisu. Te odwołania źródłowe stają się również prywatnymi danymi w kalendarzu docelowym.

Proces sprawdza prawo zapisu do dokładnie skonfigurowanego kalendarza, zmienia tylko własne wpisy, wyklucza zewnętrznych uczestników i nie wysyła zaproszeń. Odciski istniejących wpisów wykrywają ręczne zmiany. Stały znacznik operacji pomaga uniknąć duplikatów; niepewny wynik zewnętrzny jest sprawdzany w trybie odczytu zamiast ponawiany w ciemno. Odwołanie wymaga jawnego potwierdzenia i zachowuje ślad usuniętego wpisu. Wstrzymanie zapisów lub dry-run blokuje zmiany kalendarza; nie wyłącza sieciowego pobierania i analizy.

## Pochodzenie analizy i stan techniczny

Analiza wiadomości zapisuje rzeczywiście zgłoszony model, poziom rozumowania, wersję instrukcji i uzasadnienie decyzji. Brakujące historyczne pochodzenie pozostaje nieznane; samo otwarcie bazy nie dopisuje danych ani nie analizuje historii. Demo ma jawnie oznaczone fikcyjne pochodzenie i nie wywołuje modelu.

Pełny sukces synchronizacji jest oddzielony od ostatniej próby, dry-run, wstrzymania zapisów i częściowego postępu. Proces godzinowy zapisuje oczyszczone etapy techniczne, czas działania i kody błędów; pełny sukces wymaga braku automatycznych zaległości i niepewnych wyników. Propozycje czekające na decyzję rodzica nie muszą blokować technicznego sukcesu synchronizacji. Eksport Netdata w trybie odczytu zawiera zbiorcze metadane, bez treści wiadomości; zielony wskaźnik nie dowodzi, że model poprawnie zrozumiał wiadomość.

Dotychczasowe powiadomienia synchronizacji i kolejki zawierają świeży wynik ostatniego przebiegu, etap oraz stałą bezpieczną przyczynę błędu (np. `agenda_context_overflow` oznacza przekroczenie lokalnego limitu pełnego wejścia). Etykiety zawierają wyłącznie dozwolone wartości techniczne. Po odzyskaniu przyczyna ma wartość `none`, a przy nieświeżym, błędnym lub niedostępnym eksporcie `unknown`; poprzedni błąd agendy/podglądu nie jest przedstawiany jako bieżąca przyczyna. Zmiana metadanych diagnostycznych nie zmienia progów, powtórzeń ani tożsamości wykresów.

Analiza i każde narzędzie kalendarza korzystają ze skonfigurowanego modelu/poziomu, domyślnie `gpt-6.1-sol`/`high`. Początkowy odczyt listy kalendarzy może zostać ponowiony najwyżej raz po pięciu sekundach, wyłącznie przy strukturalnym błędzie nieoczekiwanej liczby zakończonych wywołań (zero lub wiele) z dokładnie oczekiwanym odczytem i argumentami. Niezakończone wywołania i inne błędy nie powodują ponowienia; tworzenie, aktualizacja i usuwanie nie podlegają tej zasadzie. Niepewne zapisy nadal wymagają sprawdzenia wyniku odczytem.

Zapis próby w przebiegu synchronizacji zawiera tylko identyfikator przebiegu/próby, czasy, model/poziom, zweryfikowaną wersję CLI, wynik, kod procesu/dozwolony kod błędu, liczniki wywołań/protokołu i flagi zgodności oczekiwanego wywołania; historia techniczna wygasa po 90 dniach. Nie zawiera identyfikatorów kalendarza, argumentów/wyników narzędzi, promptów, danych wiadomości ani surowych błędów. Panel pokazuje wynik ostatniego przebiegu, liczbę prób i odzyskanie. Nowe pola monitoringu są opcjonalne: brak/null oznacza brak danych. Wykres Netdata pokazuje bezwzględne liczniki ponowień/odzyskań z ostatnich 24 godzin i jest informacyjny; skuteczne odzyskanie nie wywołuje ostrzeżenia synchronizacji, a dotychczasowe alarmy pozostają bez zmian.

Automatyczne kopie bazy są celowo wyłączone. Wdrożenie nie kopiuje bazy; naprawa zachowuje bieżący dziennik operacji. Baza i kopie zawierają prywatne dane szkolne. Ślady analizy i usuniętych wpisów pozostają lokalnie; nie ma automatycznej ponownej analizy całej historii ani zbiorczej nieodwracalnej naprawy kalendarza.

## Granice przepływu danych prywatnych

| Dane | Miejsce w rzeczywistej instancji |
| --- | --- |
| Login i hasło Librusa | Chroniony lokalny plik oraz endpointy logowania Librusa |
| Temat, treść i nadawca wiadomości | Prywatna baza SQLite; odpowiednie przekazane pola trafiają do skonfigurowanej usługi AI |
| Kontekst zapisanych wydarzeń | Prywatna baza i wybrany kontekst weryfikacji AI/kalendarza |
| Wybrany tytuł, szczegóły i cytat źródłowy | Skonfigurowany Google Calendar przez połączoną aplikację Codex |
| Zbiorczy stan techniczny | Lokalny JSON monitoringu i opcjonalny kolektor Netdata |
| Kopie SQLite | Prywatne pliki hosta; zabezpiecz nośnik i kopie zewnętrzne |

Produkcyjne hasła, tokeny, baza SQLite, kopie, logi, dowody wykonania, rzeczywiste wiadomości i prywatne zrzuty nie należą do repozytorium. Przykłady używają zastrzeżonych domen fikcyjnych. Publiczne zrzuty zawierają wyłącznie fikcyjne dane napisane od podstaw. Demo nie czyta istniejącej konfiguracji domowej, nasłuchuje lokalnie i używa tymczasowego stanu.

Interfejs ufa lokalnemu proxy Tailscale Serve, dokładnemu hostowi i jednej skonfigurowanej tożsamości. Nie jest publiczną usługą dla wielu użytkowników. Publiczne proxy dopisujące podrobione nagłówki tożsamości narusza tę granicę zaufania. Zachowaj prywatny dostęp, skonfiguruj ACL i chroń host.

## Stan projektu

To nieoficjalny osobisty prototyp, niezależny i niezwiązany z Librusem, Google ani OpenAI. Endpointy Librusa są nieudokumentowane i mogą się zmieniać; środowisko wymaga modeli Codex i narzędzi kalendarza właściwych dla konta. Publiczny kod jest wersją konfigurowalną, a nie kopią zapasową czy dokładnym obrazem prywatnego stanu wdrożenia.

Załączniki są oznaczane do obejrzenia w Librusie; ich zawartość nie jest analizowana. Plan lekcji, oceny i wszystkie szkolne procesy nie mieszczą się w tej wąskiej integracji wiadomości. Testy i fikcyjne zrzuty obejmują określone zachowanie; nie potwierdzają zgodności z działającymi usługami, skuteczności wdrożenia, bezbłędności analizy czy poprawności kalendarza. Szczegółowy [kontrakt aplikacji](../app/CONTRACT.md) opisuje stan i zasady obsługi.

## Kontrola przed publicznym udostępnieniem

Przed publikacją uruchom `python scripts/check_public_tree.py --working-tree --all-history`. Sprawdza pliki robocze, przygotowane zmiany oraz całą lokalnie osiągalną historię i metadane Git; ignorowane, nieśledzone pliki roboczego stanu są pomijane, lecz zawartość śledzona i historyczna nadal podlega kontroli. Opcjonalny zewnętrzny JSON `{"patterns": ["literal private pattern"]}` można wskazać przez `--denylist` lub `PUBLIC_TREE_DENYLIST`. Prywatna lista musi pozostać poza repozytorium. Skaner nie jest pełnym dowodem braku sekretów i danych osobowych: sprawdź również ręcznie kod, historię, piksele obrazów i metadane.

## Źródła i jedna agenda

Jedno zadanie `gpt-6.1-sol/high`, bez narzędzi, planuje pełny ograniczony snapshot tekstów wiadomości, ogłoszeń i terminarza, obecnych działań, aktualnych własnych wpisów Calendar i zapisanych decyzji rodzica. Zwraca `create`, `link`, `update`, `review` lub `ignore`. Kod sprawdza plan, nadaje stabilne ID i używa dotychczasowego trwałego wykonawcy. Kilka źródeł tego samego obowiązku może wskazywać jedną czynność. Zastępstwa pozostają informacyjne; treści załączników, obrazów, PDF, OCR i stron z linków są wyłączone.

Zmiana źródeł, kalendarza, decyzji lub polityki planera unieważnia ponowne użycie planu. Niezmieniony przebieg pomija inference, ale nadal rozlicza operacje i sprawdza health. Przepełnienie wejścia daje widoczny błąd przed wywołaniem modelu; bez cichego obcinania. Ta sama baza SQLite zachowuje ID wydarzeń, propozycji i grup, własność, zgody, poprawione godziny, odrzucenia i dzienniki. Niepewne zmiany i anulowania wymagają sprawdzenia; brak źródła lub pozycji planu nie uprawnia do usunięcia.

`multi_source_mode` jest dawnym ustawieniem zgodności i nie steruje aktywnym silnikiem. `/sources` pokazuje źródła wspólnej czynności, a `/preview` najnowszy podgląd całej agendy, czas zebrania zapisanych źródeł i proponowane zmiany. Podgląd nie tworzy aktywnych propozycji, decyzji, operacji ani zapisów Calendar. Czyta świeży kalendarz, ale nie pobiera ponownie Librusa. Zapisany podgląd nie potwierdza bieżącej aktualności. Zobacz [obsługę i aktualizację](setup.pl.md#obsługa-jednej-agendy).

Monitoring odczytuje `agenda_engine`, aby interpretować bieżące kolejki źródeł. Eksportuje tylko ustalone wyniki, kody błędów i liczniki. Starsze bazy można odczytać bez migracji.
