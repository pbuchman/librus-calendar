# Architektura, zabezpieczenia i prywatność

[English](architecture.md) · [README](../README.pl.md) · [Konfiguracja](setup.pl.md)

System pobiera listy i treść wiadomości bezpośrednio z Librus Synergia przez HTTPS. Nie przetwarza powiadomień Gmail. Lokalna baza SQLite rozdziela wykrywanie, pobieranie całych wiadomości, analizę AI, propozycje działań, trwałe operacje, zweryfikowane kopie wydarzeń i historię stanu technicznego.

`app/librus_client.py` obsługuje wąski przepływ logowania i wiadomości; `app/sync.py` koordynuje proces z blokadą; `app/codex_runtime.py` wywołuje zalogowany Codex CLI do analizy strukturalnej i wybranych narzędzi kalendarza. `app/core.py` wymusza zasady zapisu poza modelem. Skrzynka FastAPI/Jinja w `app/web.py` pokazuje lokalne wiadomości, rozpoznane działania, zapisane wydarzenia i kolejkę sprawdzenia. Aplikacja nie udostępnia tygodniowej siatki kalendarza; siatka w dokumentacji jest wyraźnie oznaczoną osobną ilustracją fikcyjnych danych.

## Decyzje i bezpieczeństwo zapisu

Jednoznaczne przyszłe działania szkolne z potwierdzeniem w źródle mogą być automatyczne, gdy zapisy są włączone. Przygotowanie do szkoły, zajęcia klasowe i terminy oceniane są osobno dla każdego działania. Tworzenie dobrowolnych zajęć dodatkowych wymaga decyzji o udziale nawet przy jasnej dacie; nieznany zakres, niejednoznaczne daty, nakładające się lub powielone początki, niebezpieczny stan wydarzenia i odwołania wymagają sprawdzenia. Sama analiza nie może zatwierdzić odwołania ani uznać, że dziecko zapisano na dodatkowe zajęcia.

Daty używają Europe/Warsaw i rozróżniają wydarzenia godzinowe od całodniowych przypomnień i terminów. Dokładna godzina deadline'u pozostaje w tytule i opisie, a zapis w kalendarzu obejmuje jeden cały dzień. Błędne i niejednoznaczne godziny zmiany czasu są odrzucane. Dla wydarzenia godzinowego bez końca może zostać użyty wyraźnie opisany domyślny czas jednej godziny.

Każdy nowy tytuł otrzymuje jeden prefiks `[Librus]`. Opis przechowuje cytat, odwołanie do wiadomości, znacznik własności i zamrożony czas rozpoczęcia synchronizacji danej operacji. To czas przygotowania, a nie zakończenia zapisu. Te odwołania źródłowe stają się również prywatnymi danymi w kalendarzu docelowym.

Proces sprawdza prawo zapisu do dokładnie skonfigurowanego kalendarza, zmienia tylko własne wpisy, wyklucza zewnętrznych uczestników i nie wysyła zaproszeń. Odciski istniejących wpisów wykrywają ręczne zmiany. Stały znacznik operacji pomaga uniknąć duplikatów; niepewny wynik zewnętrzny jest sprawdzany w trybie odczytu zamiast ponawiany w ciemno. Odwołanie wymaga jawnego potwierdzenia i zachowuje ślad usuniętego wpisu. Wstrzymanie zapisów lub dry-run blokuje zmiany kalendarza; nie wyłącza sieciowego pobierania i analizy.

## Pochodzenie analizy i stan techniczny

Analiza wiadomości zapisuje rzeczywiście zgłoszony model, poziom rozumowania, wersję instrukcji i uzasadnienie decyzji. Brakujące historyczne pochodzenie pozostaje nieznane; samo otwarcie bazy nie dopisuje danych ani nie analizuje historii. Demo ma jawnie oznaczone fikcyjne pochodzenie i nie wywołuje modelu.

Pełny sukces synchronizacji jest oddzielony od ostatniej próby, dry-run, wstrzymania zapisów i częściowego postępu. Proces godzinowy zapisuje oczyszczone etapy techniczne, czas działania i kody błędów; pełny sukces wymaga braku automatycznych zaległości i niepewnych wyników. Propozycje czekające na decyzję rodzica nie muszą blokować technicznego sukcesu synchronizacji. Eksport Netdata w trybie odczytu zawiera zbiorcze metadane, bez treści wiadomości; zielony wskaźnik nie dowodzi, że model poprawnie zrozumiał wiadomość.

Kopie powstają przez API backupu SQLite, z kontrolą integralności i ograniczonymi uprawnieniami; domyślnie pozostaje siedem najnowszych. Baza i kopie zawierają prywatne dane szkolne. Ślady analizy i usuniętych wpisów pozostają lokalnie; nie ma automatycznej ponownej analizy całej historii ani zbiorczej nieodwracalnej naprawy kalendarza.

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
